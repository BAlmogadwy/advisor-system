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
from core.services.advisor_channel_privacy import (
    fallback_tool as channel_fallback_tool,
)
from core.services.advisor_channel_privacy import (
    is_telegram_safe_profile,
)
from core.services.advisor_channel_privacy import (
    project_history as project_channel_history,
)
from core.services.advisor_channel_privacy import (
    project_tool_result as project_channel_tool_result,
)
from core.services.advisor_channel_privacy import (
    project_tool_schemas as project_channel_tool_schemas,
)
from core.services.advisor_channel_privacy import system_prompt as channel_system_prompt
from core.services.advisor_presentations import (
    graduation_presentation_from_tool_results,
    remove_false_media_incapability,
    replacement_timetable_presentation_from_tool_results,
    timetable_presentation_from_tool_results,
)
from core.services.advisor_principal import AdvisorPrincipal
from core.services.advisor_remote_boundary import boundary_for_scope
from core.services.llm_backend import LLMError, UsageTotals, get_llm_client
from core.services.llm_remote_privacy import fold_digits
from core.services.policy_contract import requires_policy_contract
from core.services.rbac import ROLE_STUDENT
from core.services.student_helpers import normalize_code
from core.services.virtual_advisor import (
    _answer_language,
    _answer_style,
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
    "course_choice_comparison",
    "feasible_course_replacements",
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
- Follow the supplied answer_style. Understand colloquial Saudi Arabic in the
  student's input, but write Arabic answers in clear, warm Modern Standard Arabic.
  Keep official academic terms, policy wording, citations, course codes, and
  numbers precise. Do not use colloquial wording in the rendered answer.
- Arabic terminology is fixed: REGISTERED is «الجدول المسجّل فعليًا», EXPECTED_PLAN
  is «الجدول المتوقع», and any generated planning alternative is «الجدول المقترح».
  Do not use «الحالي» as a substitute for registrar evidence. A section listed in
  this system's catalogue is «شعبة مدرجة في بيانات النظام», not «شعبة مسجّلة»;
  «مسجّلة» is reserved for the student's actual registration.
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
- This system has no attendance records and does not expose a complete transcript. The
  get_student_context tool may contain failed_results with a recorded grade/mark for a
  confirmed failed course; report only those exact fields and never imply that other grades
  are available.
- recommend_courses separates genuinely new recommendations from
  already_in_current_timetable and already_in_expected_plan. Never offer a course in either
  list as something the student can add, and never call an expected-plan course registered.
  If the new recommendation list is empty, say that this system currently
  has no additional recommended course; do not repeat the student's existing courses and
  do not speculate that courses are closed/unavailable or that a credit cap caused it.
- For a choice between two to four exact course codes, call course_choice_comparison once.
  Keep prerequisite readiness, recommendation membership, direct personal unlocks, wider
  prerequisite-chain impact, the discounted downstream-importance heuristic, recorded
  timetable fit, and graduation scenarios as separate dimensions. Do not add them into one
  invented score. The weighted downstream value is this project's planning heuristic, not
  an official university priority. A course being prerequisite-ready does not by itself make
  it recommended, recorded in the section catalogue, clash-free, or permitted. A clash-free
  count is individual to that course and does not prove that several chosen courses fit
  together. Only a completed structured graduation comparison supports an exact difference
  in terms; otherwise say that timing is not determinable. Never call compared courses
  substitutes or equivalent requirements unless verified degree-plan evidence says so.
- For a request to find or verify a course replacement that both improves the academic
  graduation path and fits the complete recorded timetable, call
  feasible_course_replacements once. Pass remove_course and/or add_course only when the
  student's own wording identifies that side of the swap. A certified replacement has two
  separate proofs: a complete graduation forecast improvement and a Planner option containing
  every retained baseline section plus the replacement without clashes. Do not weaken this to
  an individual section check, and do not combine the two proofs into an invented score. If no
  certified swap is returned, describe only the bounded search that ran; never claim no feasible
  swap exists everywhere when search_truncated is true or the limitations say the search is not
  exhaustive. REGISTERED and EXPECTED_PLAN baselines must retain their exact meanings. A
  certified recorded schedule still does not prove a live offering, seat, registration
  permission, equivalence, or any portal action.
- my_timetable returns schedule_kind. EXPECTED_PLAN is a manually seeded planning
  snapshot, never actual registration: call it the expected timetable, use the expected_*
  totals, and remind the student to apply choices in the university portal. REGISTERED is
  registrar evidence. MIXED_REVIEW_REQUIRED must not be presented as one current timetable.
- Section catalogue meetings do not label or separate lecture and laboratory components.
  You may compare whole sections and their times after receiving a course code, but never
  decide whether a lab can be changed independently from its lecture or whether the two are
  linked. State that this needs confirmation from the academic department or adviser.
- For a read-only question about a named section of a named course—whether it is recorded,
  whether it clashes, or which section fits—call my_clash_free_sections. A request to PIN an
  exact section while building a timetable is different: call build_timetable_proposal with
  pinned_sections=[{course_code, section_label}] and put that course in must_take_courses, so
  the exact pinned section is required in every returned option. Do not call both tools merely
  because a pin request contains the word "section"; the proposal builder supplies the
  verified section evidence for that build. Read baseline_kind first.
  currently_registered_sections/is_current_section are registrar evidence;
  expected_plan_sections/is_expected_plan_section are only planning evidence and must be
  called expected, never registered/current. Never report that section data is missing when
  the tool returned sections_on_file greater than zero. NOT_MATCHING_STUDENT_PROFILE means
  sections are recorded in the catalogue, but none match the student's programme and study
  cohort; state that distinction and do not claim the course has no sections. Do not replace
  verified section evidence with policy-guide commentary or a generic registrar referral.
  Call catalogue rows "recorded sections", not "available sections": this result has no
  seat-availability fact.
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
- Preserve hard timetable constraints exactly. Pass explicitly mandatory courses in
  must_take_courses. Pass an exact requested section as a course_code/section_label item in
  pinned_sections—never invent or request a database section id. A pinned course in a
  pin-and-build request is also must-take: it and that exact section must appear in every
  valid option. Other sections of that course are ignored. If the course cannot be placed in
  its pinned section, report that no valid option satisfies the request; never present an
  unpinned or partial option as if it did. If a pin does not identify exactly one course and
  one section, ask one short clarification instead of building an unconstrained timetable.
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
- build_timetable_proposal returns baseline_kind plus neutral baseline_sections. REGISTERED
  permits the compatibility current_* fields; EXPECTED_PLAN uses expected_plan_* and must
  be called an expected plan, never current registration. MIXED_REVIEW_REQUIRED is a
  review state and must not be flattened into a timetable proposal.
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
  estimated_additional_terms and estimated_terms_including_planning_baseline when a planning-
  baseline timetable exists. The configured baseline can be an expected next-term plan, so
  never call it the student's actual current term. Explain that it assumes every baseline and
  simulated course is passed first time,
  uses at most 18 credits in each main term, and cannot guarantee future offerings, seats,
  section times, or registration permission. If simulation_completed is false, do not give
  simulated_terms_examined as a completion estimate; report lower_bound_additional_terms and
  name the unresolved requirements instead. Describe only each returned prerequisite or
  credit-hour blocker. Never infer that a blocker requires an extra term or special
  arrangement, or that a course has no available time, place, section, or offering. The
  18-credit value is the scenario cap, never the university's "maximum allowed" load.
- For a question about skipping, adding, or replacing a course in the planning baseline, call
  graduation_progress with remove_current_courses and/or add_current_courses. For "is there
  any baseline course I can replace to improve graduation", use search_better_replacements.
  Report the returned baseline-versus-scenario comparison. An UNRESOLVED_IMPROVEMENT means
  recorded blockers improved; it does not prove an earlier graduation term and must not be
  described as a better replacement. Replacement search only returns swaps whose complete
  forecast is earlier or changes from unresolved to completed. That search is academic only.
  When the same request also requires the modified complete timetable to fit, use
  feasible_course_replacements instead; it runs the academic proof and Planner proof as one
  bounded, deterministic read-only check. Do not say a candidate can actually be registered.
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
    re.compile(
        r"(?:(?<![؀-ۿ])(?<!لم )(?<!ما )تم[ً-ْ]*|(?<![؀-ۿ])(?<!ما )صار)\s+"
        r"(?:تسجيلك|حفظ\s+جدولك|تطبيق\s+جدولك|تأكيد\s+تسجيلك)"
    ),
    re.compile(r"(?<!ما )(?:سويت|سو[ّ]?يت|خلصت)\s+لك\s+(?:التسجيل|الجدول|الخطة)"),
    re.compile(r"(?<!لم )(?<!ما )سجلتك(?:\s+في)?"),
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
        r"(?:ابن|ابني|أبني|تبني|نبني|بناء|أنشئ|انشئ|كوّن|كون|رتب|اقترح)"
        r".*(?:جدول|شعب|تعارض|بدائل)"
    ),
    re.compile(r"(?:جدول|شعب|تعارض).*(?:ابن|أنشئ|انشئ|كوّن|كون|رتب|اقترح)"),
    re.compile(r"(?:لا\s+تغي[ّ]?ر|ثب[ّ]?ت).*?الشعب.*?(?:غي[ّ]?ر|بد[ّ]?ل).*?المقررات"),
    re.compile(r"(?:أبي|ابي|أبغى|ابغى|ابغا|ودي)(?:ك|\s+لي)?\s+(?:ب)?(?:جدول|بدائل)"),
    re.compile(r"(?:سو[ً-ْ]*ي?|سوي|زبط|ظبط|ضبط)(?:\s*لي)?\s+.*?(?:جدول|دوام)"),
    re.compile(r"(?:خل|خل[ّ]?ي)\s+.*?(?:دوامي|محاضراتي).*?(?:يومين|ثلاث(?:ة|ه)|3)\s*أيام?"),
    re.compile(r"(?:اعرض|ور[ّ]?ني).*?(?:بدائل|خيارات).*?(?:جدول|شعب|تعارض)"),
    re.compile(r"(?:بدائل|خيارات).*?(?:جدول|شعب|تعارض).*?(?:اعرض|ور[ّ]?ني)"),
    re.compile(r"جدول\s+لي\s+.*?(?:المقررات|المواد|الشعب|دوامي)"),
)

_NEGATED_TIMETABLE_CLAUSE_PATTERN = re.compile(
    r"^\s*(?:أنا\s+)?(?:مو|ما|مش)\s+(?:أبي|ابي|أبغى|ابغى|ابغا|ودي)\s+" r"(?:جدول|بدائل)(?:\s+.*)?$",
    re.IGNORECASE,
)


def _requires_timetable_proposal(question: str) -> bool:
    text = str(question or "")
    clauses = re.split(r"[،,؛;.!؟]+|\s+(?:لكن|بس)\s+", text)
    positive_clauses = [
        clause
        for clause in clauses
        if clause.strip() and not _NEGATED_TIMETABLE_CLAUSE_PATTERN.search(clause)
    ]
    positive_text = " ".join(positive_clauses)
    if any(pattern.search(positive_text) for pattern in _TIMETABLE_PROPOSAL_PATTERNS):
        return True
    # A pin is itself a request to run the proposal builder, even when the student
    # omits the word "timetable". This includes an ambiguous pin so the tool loop
    # can refuse it deterministically instead of falling through to a generic
    # section answer that silently ignores the requested constraint.
    pin, ambiguous_pin = _explicit_pin_from_question(positive_text)
    return pin is not None or ambiguous_pin


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
    re.compile(
        r"(?:أبي|ابي|أبغى|ابغى|ودي|خل|خل[ّ]?ي)\s+"
        r"(?:ال)?(?:جدول|دوام)(?:ي|ه)?(?:\s+يكون)?\s*"
        r"(?:من|بـ?)?\s*(?P<cap>\d{1,2})\s*(?:ساعة|ساعات|وحدة|وحدات)"
        r"(?:\s*(?:بالكثير|كحد\s+أقصى))?",
        re.IGNORECASE,
    ),
)
_TIMETABLE_FROM_SCRATCH_PATTERN = re.compile(
    r"(?:\bfrom\s+scratch\b|\bignore\s+(?:all\s+)?(?:my\s+)?current\b|"
    r"من\s+الصفر|من\s+البداي(?:ة|ه)|جدول\s+جديد.*?(?:من\s+البداي(?:ة|ه)|بالكامل)|"
    r"تجاهل.*?(?:جدول|شعب).*?الحالي|لا\s+تعتمد.*?(?:جدول|شعب).*?الحالي)",
    re.IGNORECASE,
)
_TIMETABLE_AROUND_CURRENT_PATTERN = re.compile(
    r"(?:\baround\s+(?:my\s+)?current\b|\bkeep\s+(?:my\s+)?current\b|"
    r"(?:احتفظ|خل[ّ]?|خلي|خلك|ثب[ّ]?ت|لا\s+تغي[ّ]?ر).*?(?:الشعب|شعبي|الجدول).*?الحالي|"
    r"(?:احتفظ|خل[ّ]?|خلي|ثب[ّ]?ت|لا\s+تغي[ّ]?ر).*?الشعب.*?(?:كما\s+ه[يو]|مثل\s+ما\s+ه[يو])|"
    r"(?:خل|خل[ّ]?ي|خلك).*?شعبي.*?(?:زي|مثل)\s+ما\s+ه[يو]|"
    r"(?:الشعب|الجدول).*?الحالي.*?(?:كما\s+ه[يو]|مثل\s+ما\s+ه[يو]))",
    re.IGNORECASE,
)

# The model may understand a pin perfectly and still omit it from the tool call. A
# section constraint is too important to leave to that choice: losing ``M2`` while
# keeping ``AI331`` produces a plausible-looking timetable that answers a different
# question. These patterns therefore extract only explicit, mechanically verifiable
# constraints from the student's own sentence. Database ids never enter this layer.
_TIMETABLE_CONSTRAINT_COURSE_PATTERN = re.compile(
    r"\b[A-Z]{2,6}\s*-?\s*\d{1,4}\b",
    re.IGNORECASE,
)
_TIMETABLE_CONSTRAINT_SECTION_PATTERN = re.compile(
    r"\b[A-Z]{1,2}\s*-?\s*\d{1,2}[A-Z]?\b",
    re.IGNORECASE,
)
_TIMETABLE_PIN_VERB_PATTERN = re.compile(
    r"(?:\bpin(?:ned|ning)?\b|ثب[ّ]?ت(?:ي|ها|ه|لي)?)",
    re.IGNORECASE,
)
_TIMETABLE_SECTION_NOUN_PATTERN = re.compile(
    r"(?:\bsections?\b|شعب(?:ة|ه|تي|تك|ته|تها|هم)?)",
    re.IGNORECASE,
)
_TIMETABLE_MUST_TAKE_PATTERN = re.compile(
    r"(?:\bmust(?:\s|-)*take\b|\bhave\s+to\s+take\b|"
    r"\bneed\s+to\s+take\b|\brequired\s+to\s+take\b|"
    r"\bmust(?:\s|-)*include\b|\bmust\s+(?:be\s+(?:included|present)|appear)\b|"
    r"\bmandatory\b|"
    r"(?:لازم|ضروري|إجباري|اجباري)(?:\s+(?:آخذ|اخذ|أنزل|انزل|أدرس|ادرس|"
    r"يكون|تكون|أضيف|اضيف))?)",
    re.IGNORECASE,
)
_TIMETABLE_NEGATED_MUST_TAKE_PATTERN = re.compile(
    r"(?:\b(?:do\s+not|don't|dont)\s+(?:have|need)\s+to\s+take\b|"
    r"(?:مو|ما|مش|ليس)\s+(?:لازم|ضروري|إجباري|اجباري)|"
    r"(?:لازم|ضروري)\s+ما\s+(?:آخذ|اخذ|أنزل|انزل))",
    re.IGNORECASE,
)
_ARABIC_CONJUNCTION_BEFORE_IDENTIFIER = re.compile(
    r"(?<![A-Za-z0-9])و(?=[A-Za-z]{1,6}\s*-?\s*\d)",
)


def _fold_constraint_text(text: str) -> str:
    """Fold digits and detach Arabic ``و`` joined directly to an identifier.

    Arabic commonly writes the conjunction without a space (``M1 وM2``). Python's
    Unicode word boundary treats both ``و`` and ``M`` as word characters, so the
    second label would otherwise be invisible and an ambiguous two-pin request
    would be misread as one exact pin. Replacing one character with one space keeps
    match spans aligned for the course/section overlap check below.
    """
    return _ARABIC_CONJUNCTION_BEFORE_IDENTIFIER.sub(" ", fold_digits(text))


def _constraint_course_codes(text: str) -> list[str]:
    """Unique course codes in textual order, folded but never inferred."""
    out: list[str] = []
    for match in _TIMETABLE_CONSTRAINT_COURSE_PATTERN.finditer(_fold_constraint_text(text)):
        code = re.sub(r"[\s-]+", "", match.group(0)).upper()
        if code and code not in out:
            out.append(code)
    return out


def _constraint_section_labels(text: str) -> list[str]:
    """Unique section labels, excluding tokens that are complete course codes."""
    folded = _fold_constraint_text(text)
    course_spans = [match.span() for match in _TIMETABLE_CONSTRAINT_COURSE_PATTERN.finditer(folded)]
    out: list[str] = []
    for match in _TIMETABLE_CONSTRAINT_SECTION_PATTERN.finditer(folded):
        start, end = match.span()
        if any(
            start < course_end and end > course_start for course_start, course_end in course_spans
        ):
            continue
        label = re.sub(r"[\s-]+", "", match.group(0)).upper()
        if label and label not in out:
            out.append(label)
    return out


def _explicit_pin_from_question(question: str) -> tuple[dict[str, str] | None, bool]:
    """Return one exact pin and whether explicit pin syntax was ambiguous.

    Repetition is not ambiguity—``AI331`` commonly appears once in the must-take
    clause and once beside its pin. Distinct values are what matter. Conversely,
    one label with two courses or two labels with one course is never resolved by
    proximity: choosing one would turn a language-model guess into a hard schedule
    constraint.
    """
    text = str(question or "")
    if not _TIMETABLE_PIN_VERB_PATTERN.search(text):
        return None, False
    codes = _constraint_course_codes(text)
    labels = _constraint_section_labels(text)
    # ``Pin AI331`` names no section and remains an ordinary course constraint.
    # Once a section noun or label is present, however, an incomplete/multi-valued
    # binding must stop the build rather than silently discarding the pin.
    section_intent = bool(labels or _TIMETABLE_SECTION_NOUN_PATTERN.search(text))
    if not section_intent:
        return None, False
    if len(codes) != 1 or len(labels) != 1:
        return None, True
    return {"course_code": codes[0], "section_label": labels[0]}, False


def _explicit_must_take_courses(question: str) -> list[str]:
    """Extract course codes from clauses that explicitly make them mandatory."""
    out: list[str] = []
    # Adversatives delimit scope; ``must take AI331, but compare CS323`` must not
    # make the comparison course mandatory. Coordinating ``and`` deliberately does
    # not split, because ``must take AI331 and CS323`` makes both mandatory.
    clauses = re.split(r"[،,؛;.!؟]+|\s+(?:but|however|لكن|بس)\s+", str(question or ""))
    for clause in clauses:
        if not _TIMETABLE_MUST_TAKE_PATTERN.search(clause):
            continue
        if _TIMETABLE_NEGATED_MUST_TAKE_PATTERN.search(clause):
            continue
        for code in _constraint_course_codes(clause):
            if code not in out:
                out.append(code)
    return out


def _normalised_constraint_codes(value: Any) -> list[str]:
    """Sanitise model-provided code lists without creating any new code."""
    values = [value] if isinstance(value, str) else value
    if not isinstance(values, list):
        return []
    out: list[str] = []
    for raw in values:
        code = re.sub(r"[\s-]+", "", fold_digits(str(raw or ""))).upper()
        if _TIMETABLE_CONSTRAINT_COURSE_PATTERN.fullmatch(code) and code not in out:
            out.append(code)
    return out


def _normalised_section_pins(value: Any) -> list[dict[str, str]]:
    """Keep only label-based pins; ids or malformed model output are discarded."""
    if not isinstance(value, list):
        return []
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, dict):
            continue
        code_values = _normalised_constraint_codes([raw.get("course_code")])
        label = re.sub(r"[\s-]+", "", fold_digits(str(raw.get("section_label") or ""))).upper()
        if (
            len(code_values) != 1
            or not _TIMETABLE_CONSTRAINT_SECTION_PATTERN.fullmatch(label)
            or _TIMETABLE_CONSTRAINT_COURSE_PATTERN.fullmatch(label)
        ):
            continue
        code = code_values[0]
        if code in seen:
            continue
        seen.add(code)
        out.append({"course_code": code, "section_label": label})
    return out


def _normalise_timetable_proposal_args(
    question: str, arguments: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """Carry explicit mode, credit and hard constraints into the planner tool.

    The model's call remains useful for conversational interpretation, but an
    explicit Must-take or section pin in the student's own text wins over an
    omitted/conflicting model argument. Only course codes and human-facing section
    labels cross this boundary; the capability resolves and validates ids locally.
    """
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

    explicit_must_take = _explicit_must_take_courses(text)
    explicit_pin, ambiguous_pin = _explicit_pin_from_question(text)
    if ambiguous_pin:
        # Internal sentinel only. The tool loop removes it before the remote
        # boundary and refuses the call locally, so neither the provider nor the
        # capability sees a made-up/unconstrained pin.
        normalised.pop("pinned_sections", None)
        normalised["_constraint_input_error"] = "AMBIGUOUS_PIN"
        return normalised, [*reasons, "ambiguous_pinned_sections"]

    requested_codes = _normalised_constraint_codes(normalised.get("course_codes"))
    must_take = _normalised_constraint_codes(normalised.get("must_take_courses"))
    for code in explicit_must_take:
        if code not in requested_codes:
            requested_codes.append(code)
        if code not in must_take:
            must_take.append(code)
    if explicit_must_take:
        reasons.append("explicit_must_take_courses")

    pins = _normalised_section_pins(normalised.get("pinned_sections"))
    if explicit_pin is not None:
        # The sentence names exactly one pin. Replace any model interpretation
        # instead of combining it with an unmentioned second hard constraint.
        pins = [explicit_pin]
        pin_code = explicit_pin["course_code"]
        if pin_code not in requested_codes:
            requested_codes.append(pin_code)
        # A natural-language "pin and build around it" requires this course to
        # occur; otherwise a solver could satisfy the pin vacuously by omitting it.
        if pin_code not in must_take:
            must_take.append(pin_code)
        reasons.append("explicit_pinned_sections")

    # Preserve the historical exact argument shape when no course constraint was
    # supplied. Several callers/tests compare this dict directly, and adding empty
    # arrays would make absence indistinguishable from an explicit empty request.
    if requested_codes:
        normalised["course_codes"] = requested_codes
    elif "course_codes" in normalised:
        normalised["course_codes"] = []
    if must_take:
        normalised["must_take_courses"] = must_take
    elif "must_take_courses" in normalised:
        normalised["must_take_courses"] = []
    if pins:
        normalised["pinned_sections"] = pins
    elif "pinned_sections" in normalised:
        normalised["pinned_sections"] = []

    return normalised, reasons


_INTERNAL_OUTPUT_MARKERS = re.compile(
    r"(?:source_leaves_unresolved|decision_use|PROHIBITED_FOR_DECISION|"
    r"PARTIALLY_EVALUABLE|PERMITTED_WITH_USER_PROVIDED_INPUTS|EXPLANATORY_ONLY|"
    r"reason_code|NOT_ON_FILE|NOT_MATCHING_STUDENT_PROFILE|OMITTED_IN_THIS_VARIANT|"
    r"listed_as_prerequisite_for|sole_remaining_prerequisite(?:_for)?|"
    r"on_prerequisite_chain_of|build_timetable_proposal|recommend_courses|"
    r"course_choice_comparison|feasible_course_replacements|"
    r"graduation_progress|my_clash_free_sections|my_timetable|my_progress|"
    r"my_plan_by_term|get_student_context|lookup_course|course_prerequisites|"
    r"why_course_locked|policy_lookup|my_advisor|max_credits|"
    r"around_current|from_scratch|must_take_courses|pinned_sections|"
    r"section_label|AMBIGUOUS_PIN)",
    re.IGNORECASE,
)


def _internal_output_markers(answer: str) -> list[str]:
    return sorted({match.group(0) for match in _INTERNAL_OUTPUT_MARKERS.finditer(answer or "")})


def _prefer_arabic_course_names_in_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Localise course labels in one Arabic response without changing catalogue data."""
    from core.services.student_sections import arabic_term_section_course_names

    copied = copy.deepcopy(payload)
    codes: set[str] = set()

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            candidate = normalize_code(value.get("course_code") or value.get("code") or "")
            if re.fullmatch(r"[A-Z]{1,6}\d{1,4}", candidate):
                codes.add(candidate)
            name_map = value.get("nameOf")
            if isinstance(name_map, dict):
                codes.update(
                    code
                    for raw_code in name_map
                    if re.fullmatch(r"[A-Z]{1,6}\d{1,4}", (code := normalize_code(raw_code)))
                )
            for child in value.values():
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)

    collect(copied)
    arabic_names = arabic_term_section_course_names(codes)
    if not arabic_names:
        return copied

    def apply(value: Any) -> None:
        if isinstance(value, dict):
            code = normalize_code(value.get("course_code") or value.get("code") or "")
            arabic_name = arabic_names.get(code, "")
            if arabic_name:
                if "course_name" in value:
                    value["course_name"] = arabic_name
                if "name" in value:
                    value["name"] = arabic_name
            name_map = value.get("nameOf")
            if isinstance(name_map, dict):
                for raw_code in list(name_map):
                    localised = arabic_names.get(normalize_code(raw_code), "")
                    if localised:
                        name_map[raw_code] = localised
            for child in value.values():
                apply(child)
        elif isinstance(value, list):
            for child in value:
                apply(child)

    apply(copied)
    return copied


def _humanise_internal_output_markers(answer: str, language: str) -> str:
    """Last-resort cleanup after a bounded rewrite still leaks schema labels."""
    replacements_ar = {
        "source_leaves_unresolved": "لم يحسم المصدر هذه النقطة",
        "decision_use": "نطاق الاستفادة من الدليل",
        "prohibited_for_decision": "لا تكفي هذه القاعدة للحكم على حالة الطالب",
        "partially_evaluable": "لا يمكن التحقق إلا من جزء من الحالة",
        "permitted_with_user_provided_inputs": "يتطلب التحقق بيانات إضافية من الطالب",
        "explanatory_only": "هذه القاعدة للتوضيح فقط",
        "reason_code": "سبب النتيجة",
        "not_on_file": "غير مدرج في بيانات النظام",
        "not_matching_student_profile": "لا يطابق برنامج الطالب أو شطر الدراسة",
        "omitted_in_this_variant": "غير مدرج في هذا الخيار وحده",
        "listed_as_prerequisite_for": "مقررات تذكره كمتطلب سابق",
        "sole_remaining_prerequisite": "المتطلب السابق الوحيد المتبقي",
        "sole_remaining_prerequisite_for": (
            "مقررات لم يتبق من متطلباتها السابقة غير المستوفاة سوى هذا المقرر"
        ),
        "on_prerequisite_chain_of": "مقررات يدخل هذا المقرر في سلسلة متطلباتها السابقة",
        "build_timetable_proposal": "إنشاء جدول مقترح",
        "recommend_courses": "إعداد توصية المقررات",
        "course_choice_comparison": "مقارنة خيارات المقررات",
        "feasible_course_replacements": "فحص الاستبدال الأكاديمي والجدولي",
        "graduation_progress": "محاكاة التقدم نحو التخرج",
        "my_clash_free_sections": "فحص تعارضات الشُعب",
        "my_timetable": "بيانات جدولك",
        "my_progress": "تقدمك الأكاديمي",
        "my_plan_by_term": "خطة المقررات حسب الفصل",
        "get_student_context": "بياناتك الأكاديمية",
        "lookup_course": "بيانات المقرر",
        "course_prerequisites": "متطلبات المقرر",
        "why_course_locked": "تحليل المتطلبات غير المستوفاة للمقرر",
        "policy_lookup": "البحث في اللوائح المعتمدة",
        "my_advisor": "بيانات المرشد",
        "max_credits": "بيانات الحد الأعلى للساعات",
        "around_current": "إنشاء المقترح مع الإبقاء على الجدول المرجعي",
        "from_scratch": "إنشاء المقترح من البداية",
        "must_take_courses": "المقررات المطلوبة في كل بديل",
        "pinned_sections": "قيود الشعب المثبّتة",
        "section_label": "رمز الشعبة",
        "ambiguous_pin": "طلب تثبيت الشعبة غير مكتمل ويحتاج إلى توضيح",
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
        "not_matching_student_profile": "not matched to the student's programme or study cohort",
        "omitted_in_this_variant": "omitted only from this alternative",
        "listed_as_prerequisite_for": "courses that list it as a prerequisite",
        "sole_remaining_prerequisite": "the sole remaining prerequisite",
        "sole_remaining_prerequisite_for": "courses for which it is the sole remaining prerequisite",
        "on_prerequisite_chain_of": "courses whose prerequisite chain includes it",
        "build_timetable_proposal": "the timetable proposal builder",
        "recommend_courses": "the course recommendation engine",
        "course_choice_comparison": "the course-choice comparison",
        "feasible_course_replacements": "the academic and timetable replacement check",
        "graduation_progress": "the graduation-progress simulation",
        "my_clash_free_sections": "the clash-free section check",
        "my_timetable": "your timetable data",
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
        "must_take_courses": "courses required in every option",
        "pinned_sections": "exact pinned-section constraints",
        "section_label": "the section identifier",
        "ambiguous_pin": "a section pin that needs clarification",
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
    text = re.sub(r"`([^`\n]+)`", r"\1", text)
    if language == "Arabic":
        # Removing an internal marker from a sentence such as «السجل يحمل …» can
        # leave two individually correct fragments joined into broken Arabic.
        # Smooth the surrounding carrier phrase as part of the same last-resort
        # sanitation pass; this text can reach the student when a bounded model
        # rewrite leaks the marker twice.
        text = re.sub(r"السجل\s+يحمل\s+(?=لم يحسم المصدر)", "", text)
        text = text.replace(
            f"{unresolved} و{prohibited}",
            f"{unresolved}، و{prohibited}",
        )
    return text


_SECTION_REQUEST_WORD_PATTERN = re.compile(
    r"(?:\bsection\b|ش[ً-ْ]*عب(?:ة|ه|تي|تك|ته|تها|هم)?)", re.IGNORECASE
)
_SECTION_CODE_TOKEN_PATTERN = re.compile(r"\b[A-Z]\d{1,3}\b", re.IGNORECASE)


def _requires_section_check(question: str) -> bool:
    """Whether a named-course question requires recorded-section evidence.

    This is the same kind of grounding gate as timetable/graduation checks below,
    not an intent router. It prevents a repeated question from being answered by
    copying an earlier assistant mistake out of conversation history.
    """
    text = str(question or "")
    # An exact pin is checked by the same builder that must honour it. Requiring
    # my_clash_free_sections as well creates two independent interpretations of
    # the label and used to let the second, read-only comparison stand in for a
    # proposal that had silently dropped the pin. Plain section questions still
    # use the clash checker below.
    if _requires_timetable_proposal(text):
        return False
    return bool(
        _COURSE_CODE_TOKEN_PATTERN.search(text)
        and (_SECTION_REQUEST_WORD_PATTERN.search(text) or _SECTION_CODE_TOKEN_PATTERN.search(text))
    )


_GRADUATION_PROGRESS_PATTERN = re.compile(
    r"(?:\bgraduat(?:e|ing|ion)\b|تخر(?:ج|ّج)|التخرج|"
    r"(?:مدة|موعد|وقت).*?إنهاء\s+(?:خطتي|الخطة|متطلبات\s+الخطة)|"
    r"(?:كم|وش).*?(?:ترم|فصل).*?(?:باقي|متبق)|"
    r"(?:باقي|متبق).*?(?:كم).*?(?:ترم|فصل)|"
    r"(?:متى|كم).*?(?:أخلص|اخلص|أنهي|انهي)(?:\s+من)?\s+"
    r"(?:خطتي|الخطة|متطلبات\s+الخطة|دراستي|الجامعة)|"
    r"(?:متى|كم|باقي).*?(?:أصير|اصير|أكون|اكون)\s+خريج|"
    r"(?:أخلص|اخلص|أنهي|انهي).*?(?:خطتي|الخطة|متطلبات\s+الخطة)|"
    r"(?:أبي|ابي|أبغى|ابغى|ودي).*?(?:أخلص|اخلص).*?الخطة)",
    re.IGNORECASE,
)


def _requires_graduation_progress(question: str) -> bool:
    return bool(_GRADUATION_PROGRESS_PATTERN.search(question or ""))


_COURSE_CODE_EXPR = r"[A-Z]{2,6}\s*-?\s*\d{1,4}"
_COURSE_CODE_TOKEN_PATTERN = re.compile(rf"\b{_COURSE_CODE_EXPR}\b", re.IGNORECASE)
_CURRENT_COURSE_CHANGE_PATTERN = re.compile(
    r"(?:\b(?:do\s+not|don['’]t|did\s+not|didn['’]t|not)\s+take\b|"
    r"\b(?:skip|drop|remove|replace|swap|defer)\b|\binstead\s+of\b|"
    r"(?:ما\s*(?:آخذ|اخذ|أخذت|اخذت|خذت|نزلت|أنزل|انزل|باخذ|بآخذ|بنزل)|"
    r"(?:ماني|مو)\s*(?:ماخذ|آخذ|اخذ|باخذ|بآخذ|منزل)|لم\s+(?:آخذ|اخذ|أنزل|انزل)|"
    r"بدل|بدال|استبدل|أستبدل|استبدال|أبدل|ابدل|أغير|اغير|"
    r"أحذف|احذف|حذف|أشيل|اشيل|شيل|شلت|أكنسل|اكنسل|ألغي|الغي|"
    r"أؤجل|اؤجل|تأجيل|أترك|اترك))",
    re.IGNORECASE,
)
_OPEN_REPLACEMENT_PATTERN = re.compile(
    r"(?:\b(?:which|what|any)\s+(?:current\s+)?course\b.*\b(?:replace|swap)\b|"
    r"\b(?:replace|swap)\b.*\b(?:which|what|any)\s+(?:current\s+)?course\b|"
    r"(?:أي|اي|وش|فيه|هناك|يوجد).*?(?:مقرر|مادة).*?"
    r"(?:استبدل|أستبدل|أبدل|ابدل|أغير|اغير|أشيل|اشيل)|"
    r"(?:استبدل|أستبدل|أبدل|ابدل|أغير|اغير|أشيل|اشيل).*?"
    r"(?:أي|اي|وش|مقرر|مادة))",
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


_REPLACEMENT_ACTION_PATTERN = re.compile(
    r"(?:\b(?:replace|swap|substitute|replacement|switch|drop)\b|"
    r"\b(?:instead\s+of|in\s+place\s+of)\b|"
    r"استبدال|استبدل|أستبدل|بديل|تبديل|بدلت|أبدل|ابدل|أبدّل|ابدّل|أغير|اغير|"
    r"أشيل|اشيل|شيل|شلت|أحذف|احذف|ألغي|الغي|بدل|بدال|مكان)",
    re.IGNORECASE,
)
_REPLACEMENT_TIMETABLE_PROOF_PATTERN = re.compile(
    r"(?:\b(?:timetable|schedule|section|clash|conflict)\b|"
    r"without\s+(?:a\s+)?(?:clash|conflict)|"
    r"جدول|شعب(?:ة|ه|تي|تك|ي)?|تعارض|بدون\s+تعارض|"
    r"يدخل\s+(?:في|مع)|يركب\s+(?:في|مع|على)|"
    r"(?:يضبط|يمشي|يتوافق)\s+(?:في|مع|على)\s+(?:جدولي|دوامي|باقي\s+شعبي)|"
    r"(?:يناسب|يلائم)\s+(?:جدولي|دوامي)|دوام)",
    re.IGNORECASE,
)
_REPLACEMENT_ENGLISH_FIT_PATTERN = re.compile(r"\bfit(?:s|ting)?\b", re.IGNORECASE)
_REPLACEMENT_NON_TIMETABLE_FIT_PATTERN = re.compile(
    r"(?:\b(?:career|degree(?:\s+plan)?|academic(?:\s+(?:plan|goals?))?|goals?|"
    r"interests?|budget|sentence|description|requirements?|prerequisites?)\b"
    r"[^.?!]{0,32}\bfit(?:s|ting)?\b|"
    r"\bfit(?:s|ting)?\b[^.?!]{0,64}\b"
    r"(?:career|degree(?:\s+plan)?|academic(?:\s+(?:plan|goals?))?|goals?|"
    r"interests?|budget|sentence|description|requirements?|prerequisites?)\b)",
    re.IGNORECASE,
)
_REPLACEMENT_TEXT_EDIT_PATTERN = re.compile(
    r"(?:\b(?:word|sentence|paragraph|text|description|title|label|wording|phrase)\b|"
    r"كلم(?:ة|ه)|جمل(?:ة|ه)|فقرة|نص|وصف|عنوان|صياغة)",
    re.IGNORECASE,
)
_SAME_COURSE_SECTION_SWAP_PATTERN = re.compile(
    r"(?:\b(?:replace|swap|switch)\b[^.?!]{0,80}\bsection\b|"
    r"\bsection\b[^.?!]{0,80}\b(?:replace|swap|switch)\b|"
    r"(?:استبدل|أستبدل|أبدل|ابدل|أغير|اغير|بدّل|بدل)[^.?!]{0,80}شعب(?:ة|ه)|"
    r"شعب(?:ة|ه)[^.?!]{0,80}(?:استبدل|أستبدل|أبدل|ابدل|أغير|اغير|بدّل|بدل))",
    re.IGNORECASE,
)
_REPLACEMENT_ADD_TARGET_PATTERNS = (
    re.compile(
        rf"\b(?:take|add|include)\s+(?P<add>{_COURSE_CODE_EXPR})\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"(?:آخذ|اخذ|أنزل|انزل|أضيف|اضيف|أحط|احط)\s+"
        rf"(?:مقرر\s+|مادة\s+)?(?P<add>\b{_COURSE_CODE_EXPR}\b)",
        re.IGNORECASE,
    ),
)


def _requires_feasible_course_replacements(question: str) -> bool:
    """Require the two-gate swap engine when the timetable must survive a change.

    This remains an evidence gate, not a role or intent classifier.  The model can
    still understand and answer the request, but it may not claim that a modified
    current timetable fits from an individual section check or an academic-only
    graduation scenario.
    """
    text = str(question or "")
    if not _REPLACEMENT_ACTION_PATTERN.search(text):
        return False
    # The adviser can receive pasted editing requests too. Text-object words are
    # explicit counter-evidence: do not turn "replace this paragraph if it fits"
    # into a degree-plan simulation. Open academic questions such as "what can I
    # replace without a clash?" remain valid even when no course code is named.
    if _REPLACEMENT_TEXT_EDIT_PATTERN.search(text):
        return False
    # Changing M1 to M2 for the same course is a section choice, not an academic
    # course replacement. It belongs to the timetable/section evidence path.
    if (
        len(_comparison_course_codes(text)) == 1
        and _constraint_section_labels(text)
        and _SAME_COURSE_SECTION_SWAP_PATTERN.search(text)
    ):
        return False
    if _REPLACEMENT_TIMETABLE_PROOF_PATTERN.search(text):
        return True

    # In a course-replacement question, bare "fit", "fits", and "fitting"
    # commonly mean fitting the recorded timetable. Require explicit course
    # context and reject stated non-timetable meanings rather than treating every
    # occurrence of "fits" in ordinary prose as a two-gate scheduling request.
    has_course_context = bool(
        _COURSE_CODE_TOKEN_PATTERN.search(text)
        or re.search(r"\b(?:course|class|section)\b", text, re.IGNORECASE)
    )
    return bool(
        has_course_context
        and _REPLACEMENT_ENGLISH_FIT_PATTERN.search(text)
        and not _REPLACEMENT_NON_TIMETABLE_FIT_PATTERN.search(text)
    )


_COURSE_COMPARISON_CUE_PATTERN = re.compile(
    r"(?:\bcompare\b|\bcomparison\b|\bversus\b|\bvs\.?\b|"
    r"\bbetter\b|\bbest\b|\bwhich\s+(?:one|course)\b|\bor\b|"
    r"\brank\b|\bprioriti[sz]e\b|\binstead\s+of\b|"
    r"قارن|مقارن(?:ة|ه)|أيهم|ايهم|أي\s+(?:واحد|وحدة|مقرر|مادة)|"
    r"وش\s+(?:أفضل|أحسن|أنسب|أولى)|و?الأفضل|أحسن|أنسب|أولى|"
    r"ولا|أو|رت[ّ]?ب|بدل|بدال)",
    re.IGNORECASE,
)
_COURSE_COMPARISON_TIMETABLE_OBJECTIVE = re.compile(
    r"(?:\b(?:timetable|schedule|section|clash|conflict|time)\b|"
    r"جدول|شعب(?:ة|ه)?|تعارض|وقت|دوام)",
    re.IGNORECASE,
)
_COURSE_COMPARISON_UNLOCK_OBJECTIVE = re.compile(
    r"(?:\b(?:unlock|impact|priority|prerequisite\s+chain|opens?\s+more)\b|"
    r"يفتح|تفتح|أثر|تأثير|أولوية|اولوي(?:ة|ه)|سلسلة\s+المتطلبات|الخطة)",
    re.IGNORECASE,
)
_COURSE_COMPARISON_DEFERRAL_OBJECTIVE = re.compile(
    r"(?:\b(?:defer|delay|postpone|skip).*?(?:less|least|harm|impact)|"
    r"تأجيل|أؤجل|اؤجل|أج[ّ]?ل|ضرر|يضر|يتأخر|تأخير)",
    re.IGNORECASE,
)


def _comparison_course_codes(question: str) -> list[str]:
    """Return distinct explicit course codes in the student's textual order."""
    codes: list[str] = []
    for match in _COURSE_CODE_TOKEN_PATTERN.finditer(_fold_constraint_text(str(question or ""))):
        code = _normalise_course_code(match.group(0))
        if code and code not in codes:
            codes.append(code)
    return codes


def _requires_course_choice_comparison(question: str) -> bool:
    """Ground an explicit 2–4 course choice in the deterministic comparator."""
    text = str(question or "")
    # Concrete add/remove graduation scenarios and actual timetable builds already
    # have stronger deterministic engines. The comparator owns the choice question,
    # not every sentence that happens to contain "or".
    if _requires_graduation_what_if(text) or _requires_timetable_proposal(text):
        return False
    codes = _comparison_course_codes(question)
    return 2 <= len(codes) <= 4 and bool(_COURSE_COMPARISON_CUE_PATTERN.search(text))


def _normalise_course_comparison_args(
    question: str, arguments: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """Bind the model call to the exact candidates and objective in the question."""
    normalised = dict(arguments or {})
    reasons: list[str] = []
    if "academic_year" in normalised or "term" in normalised:
        # The model never chooses the comparison period. A period explicitly
        # written by the student is parsed separately and carried only through
        # trusted local execution context.
        normalised.pop("academic_year", None)
        normalised.pop("term", None)
        reasons.append("discarded_model_term_override")
    codes = _comparison_course_codes(question)
    if 2 <= len(codes) <= 4 and normalised.get("course_codes") != codes:
        normalised["course_codes"] = codes
        reasons.append("explicit_course_codes")

    text = str(question or "")
    if _requires_graduation_progress(text) or _COURSE_COMPARISON_DEFERRAL_OBJECTIVE.search(text):
        objective = "graduation"
    elif _COURSE_COMPARISON_TIMETABLE_OBJECTIVE.search(text):
        objective = "timetable_fit"
    elif _COURSE_COMPARISON_UNLOCK_OBJECTIVE.search(text):
        objective = "unlock_impact"
    else:
        # No stated priority means a balanced comparison. Letting the model pick
        # graduation or timetable fit here would silently answer a different
        # question and make otherwise identical prompts nondeterministic.
        objective = "balanced"
    if normalised.get("objective") != objective:
        normalised["objective"] = objective
        reasons.append("explicit_objective")
    return normalised, reasons


_EXPLICIT_COMPARISON_TERM_PATTERN = re.compile(
    r"(?<!\d)(?P<academic_year>\d{4})\s*[/؍]\s*(?P<term>[1-3])(?!\d)"
)
_COMPARISON_TERM_TARGET_BEFORE_PATTERN = re.compile(
    r"(?:\b(?:for|in)\s+(?:term\s+)?|"
    r"(?:في|لـ?|للترم|للفصل)\s*)$",
    re.IGNORECASE,
)
_COMPARISON_TERM_TARGET_AFTER_PATTERN = re.compile(
    r"^\s*[, :]?\s*(?:please\s+)?(?:compare|rank)\b|"
    r"^\s*[, :]?\s*(?:قارن|رت[ّ]?ب)\b",
    re.IGNORECASE,
)
_COMPARISON_CLAUSE_BEFORE_TERM_PATTERN = re.compile(
    r"(?:\b(?:compare|comparison|rank)\b|قارن|مقارن(?:ة|ه)|رت[ّ]?ب)",
    re.IGNORECASE,
)
_COMPARISON_TERM_NEGATION_PATTERN = re.compile(
    r"(?:\b(?:not|except)\s+(?:for\s+)?|(?:مو|ليس|لا)\s*(?:في|لـ?|للترم|للفصل)?\s*)$",
    re.IGNORECASE,
)


def _explicit_comparison_year_term(question: str) -> tuple[int, int] | None:
    """Parse a conservative student-authored Hijri ``year/term`` reference.

    Digit folding supports both Western and Arabic numerals. The intentionally
    narrow slash form prevents unrelated years and prose generated by the model
    from silently moving a comparison to another timetable baseline.
    """
    text = fold_digits(str(question or ""))
    matches = list(_EXPLICIT_COMPARISON_TERM_PATTERN.finditer(text))
    if len(matches) != 1:
        # Multiple periods normally describe history or a contrast. Without a
        # full temporal parser, selecting either one risks moving the verified
        # baseline away from what the student meant.
        return None
    for match in reversed(matches):
        academic_year = int(match.group("academic_year"))
        term = int(match.group("term"))
        prefix = text[max(0, match.start() - 64) : match.start()]
        suffix = text[match.end() : match.end() + 64]
        if _COMPARISON_TERM_NEGATION_PATTERN.search(prefix):
            continue
        target_before = bool(_COMPARISON_TERM_TARGET_BEFORE_PATTERN.search(prefix))
        target_after = bool(_COMPARISON_TERM_TARGET_AFTER_PATTERN.search(suffix))
        # A bare historical phrase such as "I studied in 1447/2" must not
        # redirect a later comparison. "Compare ... in 1447/2" is different:
        # the comparison cue occurs in the same clause before the explicit term.
        same_clause_prefix = re.split(r"[.;?!؟]", prefix)[-1]
        comparison_before = bool(_COMPARISON_CLAUSE_BEFORE_TERM_PATTERN.search(same_clause_prefix))
        if (
            1400 <= academic_year <= 1500
            and term in (1, 2, 3)
            and (
                target_after
                or (target_before and comparison_before)
                or re.search(
                    r"(?:\b(?:comparison|compare|rank)\b|مقارن(?:ة|ه)|قارن|رت[ّ]?ب)"
                    r"[^.;?!؟]{0,64}\b(?:for|in)\s+(?:term\s+)?$",
                    same_clause_prefix,
                    re.IGNORECASE,
                )
            )
        ):
            return academic_year, term
    return None


_ARABIC_INSTEAD_PATTERN = re.compile(
    rf"(?P<add>\b{_COURSE_CODE_EXPR}\b)\s+"
    rf"(?:بدل(?:اً|ا)?(?:\s+من)?|بدال|مكان|عوض(?:اً|ا)?\s+عن)\s+"
    rf"(?P<remove>\b{_COURSE_CODE_EXPR}\b)",
    re.IGNORECASE,
)
_ENGLISH_INSTEAD_PATTERN = re.compile(
    rf"(?P<add>\b{_COURSE_CODE_EXPR}\b)\s+instead\s+of\s+" rf"(?P<remove>\b{_COURSE_CODE_EXPR}\b)",
    re.IGNORECASE,
)
_ENGLISH_REPLACE_PATTERN = re.compile(
    rf"\b(?:replace|swap|substitute)\s+(?P<remove>{_COURSE_CODE_EXPR})\s+"
    rf"(?:with|for)\s+(?P<add>{_COURSE_CODE_EXPR})\b|"
    rf"\bswitch\s+(?P<remove_switch>{_COURSE_CODE_EXPR})\s+(?:to|with|for)\s+"
    rf"(?P<add_switch>{_COURSE_CODE_EXPR})\b|"
    rf"\buse\s+(?P<add_use>{_COURSE_CODE_EXPR})\s+as\s+(?:a\s+)?replacement\s+for\s+"
    rf"(?P<remove_use>{_COURSE_CODE_EXPR})\b|"
    rf"\bdrop\s+(?P<remove_drop>{_COURSE_CODE_EXPR})\s+(?:and|then)\s+"
    rf"(?:take|add)\s+(?P<add_drop>{_COURSE_CODE_EXPR})\b",
    re.IGNORECASE,
)
_ARABIC_TAKE_INSTEAD_PATTERN = re.compile(
    rf"(?:آخذ|اخذ|أنزل|انزل|أحط|احط)\s+"
    rf"(?P<add>{_COURSE_CODE_EXPR})\s+(?:بدل|بدال|مكان)\s+"
    rf"(?P<remove>{_COURSE_CODE_EXPR})\b",
    re.IGNORECASE,
)
_ARABIC_REPLACE_PATTERN = re.compile(
    rf"(?:استبدال|استبدل|أستبدل|بدلت|أبدل|ابدل|أبدّل|ابدّل|أغير|اغير)\s+"
    rf"(?P<remove>\b{_COURSE_CODE_EXPR}\b)\s+"
    rf"(?:ب|بـ|مع)\s*(?P<add>\b{_COURSE_CODE_EXPR}\b)",
    re.IGNORECASE,
)
_ARABIC_REMOVE_ADD_PATTERN = re.compile(
    rf"(?:أشيل|اشيل|شيل|شلت|أحذف|احذف|ألغي|الغي|أكنسل|اكنسل)\s+"
    rf"(?P<remove>\b{_COURSE_CODE_EXPR}\b).*?"
    rf"(?:وأحط|واحط|وحط|حطيت|وأضيف|واضيف|وأبدله\s+ب|وابدله\s+ب)\s*"
    rf"(?P<add>\b{_COURSE_CODE_EXPR}\b)",
    re.IGNORECASE,
)
_ARABIC_REVERSED_INSTEAD_PATTERN = re.compile(
    rf"(?:بدال|بدل|مكان)\s+(?P<remove>\b{_COURSE_CODE_EXPR}\b).*?"
    rf"(?:آخذ|اخذ|أنزل|انزل|أحط|احط|حطيت)\s+"
    rf"(?P<add>\b{_COURSE_CODE_EXPR}\b)",
    re.IGNORECASE,
)
_ARABIC_OMISSION_PATTERN = re.compile(
    rf"(?:ما\s*(?:آخذ|اخذ|أخذت|اخذت|خذت|نزلت|أنزل|انزل|باخذ|بآخذ|بنزل)|"
    rf"(?:ماني|مو)\s*(?:ماخذ|آخذ|اخذ|باخذ|بآخذ|منزل)|"
    rf"لم\s+(?:آخذ|اخذ|أنزل|انزل)|أحذف|احذف|حذف|أشيل|اشيل|شيل|شلت|"
    rf"أكنسل|اكنسل|ألغي|الغي|أؤجل|اؤجل|أترك|اترك)\s+"
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
    return re.sub(r"[\s-]+", "", fold_digits(str(value or ""))).upper()


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
        _ARABIC_TAKE_INSTEAD_PATTERN,
        _ARABIC_REPLACE_PATTERN,
        _ARABIC_REMOVE_ADD_PATTERN,
        _ARABIC_REVERSED_INSTEAD_PATTERN,
    ):
        match = pattern.search(text)
        if match:
            groups = match.groupdict()
            remove = next(
                (
                    groups.get(key)
                    for key in ("remove", "remove_switch", "remove_use", "remove_drop")
                    if groups.get(key)
                ),
                "",
            )
            add = next(
                (
                    groups.get(key)
                    for key in ("add", "add_switch", "add_use", "add_drop")
                    if groups.get(key)
                ),
                "",
            )
            normalised["remove_current_courses"] = [_normalise_course_code(remove)]
            normalised["add_current_courses"] = [_normalise_course_code(add)]
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


def _normalise_feasible_replacement_args(
    question: str, arguments: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """Bind both sides of a timetable-certified swap to the student's words.

    The remote model is never allowed to choose a course silently.  A missing side
    remains missing, which asks the deterministic service to search that side.  A
    course mentioned only by the model is discarded before the capability runs.
    """
    # A certified replacement is defined against the timetable snapshot for the
    # server-configured chat term.  The model may identify explicit course sides,
    # but it may not silently redirect that proof to another year or term.
    normalised: dict[str, Any] = {}
    reasons: list[str] = []
    if arguments.get("academic_year") not in (None, "") or arguments.get("term") not in (
        None,
        "",
    ):
        reasons.append("discarded_model_term_override")
    scenario, scenario_reason = _normalise_graduation_scenario_args(question, {})
    removed = [
        _normalise_course_code(value)
        for value in scenario.get("remove_current_courses") or []
        if _normalise_course_code(value)
    ]
    added = [
        _normalise_course_code(value)
        for value in scenario.get("add_current_courses") or []
        if _normalise_course_code(value)
    ]

    text = str(question or "")
    codes = _comparison_course_codes(text)
    if not removed and not added and len(codes) == 1:
        explicit_add = next(
            (
                _normalise_course_code(match.group("add"))
                for pattern in _REPLACEMENT_ADD_TARGET_PATTERNS
                if (match := pattern.search(text))
            ),
            "",
        )
        if explicit_add:
            added = [explicit_add]
            scenario_reason = "explicit_add_target"
        elif _REPLACEMENT_ACTION_PATTERN.search(text):
            # "Replace DS341 with the best course that fits" identifies the
            # removed side; the service, not the model, searches for the addition.
            removed = [codes[0]]
            scenario_reason = "explicit_remove_target"

    if removed:
        normalised["remove_course"] = removed[0]
    if added:
        normalised["add_course"] = added[0]
    if scenario_reason:
        reasons.append(scenario_reason)
    if arguments.get("remove_course") and not removed:
        reasons.append("discarded_unstated_remove_course")
    if arguments.get("add_course") and not added:
        reasons.append("discarded_unstated_add_course")
    return normalised, reasons


_SECTION_DATA_MISSING_CLAIM = re.compile(
    r"(?:لا\s+توجد\s+(?:شعب|بيانات)|"
    r"لا\s+(?:يحتوي|يوجد).*?(?:سجل|شعب)|"
    r"ما\s+(?:فيه|في|عندنا)\s+(?:أي\s+)?(?:شعب|بيانات(?:\s+(?:عن|لـ?)\s+الشعب)?)|"
    r"(?:الشعب|بيانات\s+الشعب).*?مو\s+(?:موجودة|مسجلة|متوفرة)|"
    r"غير\s+مسجل(?:ة|ه)?.*?(?:شعب|النظام)|"
    r"\bno\s+(?:recorded\s+)?sections?\b|"
    r"\bno\s+section\s+data\b|"
    r"\bnot\s+(?:recorded|on\s+file|in\s+the\s+system)\b|"
    r"\bNOT_ON_FILE\b)",
    re.IGNORECASE | re.DOTALL,
)

_SECTION_POLICY_DIVERSION = re.compile(
    r"(?:الدليل\s+الإرشادي.*?(?:لا|ما)\s+يذكر.*?(?:الشعب|الشُعب)|"
    r"student\s+guide.*?(?:does\s+not|doesn't).*?(?:sections?|availability))",
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
    verified = _verified_section_results(tool_results)
    has_recorded_sections = any(
        int(row.get("sections_on_file") or 0) > 0
        or int(row.get("recorded_sections_on_file") or 0) > 0
        for row in verified
    )
    missing_claim = has_recorded_sections and bool(_SECTION_DATA_MISSING_CLAIM.search(answer or ""))
    policy_diversion = bool(verified) and bool(_SECTION_POLICY_DIVERSION.search(answer or ""))
    return missing_claim or policy_diversion


def _apply_saudi_register(answer: str, language: str, answer_style: str) -> str:
    """Keep deterministic responses in the portal's formal university register.

    Saudi colloquial language is still recognised in student input, but output is
    deliberately Modern Standard Arabic so labels, policy wording, and academic
    distinctions remain consistent across the student portal.
    """
    return str(answer or "")


def _safe_section_answer(
    language: str,
    tool_results: list[dict[str, Any]],
    answer_style: str = "",
) -> str:
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
    baseline_kind = str(result.get("baseline_kind") or "REGISTERED")
    if baseline_kind == "MIXED_REVIEW_REQUIRED":
        answer = (
            "تتضمن بيانات هذا الفصل صفوفًا من الجدول المسجّل فعليًا وصفوفًا من "
            "الجدول المتوقع، ولم يتمكن النظام من فصل المصدرين لهذا الفحص. راجع "
            "بيانات الجدولين قبل مقارنة الشُعب."
            if language == "Arabic"
            else "This term contains both registrar and expected-plan rows, so a reliable "
            "section comparison cannot be made until the timetable source is reviewed."
        )
        return _apply_saudi_register(answer, language, answer_style)
    expected_baseline = baseline_kind == "EXPECTED_PLAN"
    lines: list[str] = []
    for course in result.get("courses") or []:
        if not isinstance(course, dict):
            continue
        code = str(course.get("course_code") or "").strip()
        count = int(course.get("sections_on_file") or 0)
        recorded_count = int(course.get("recorded_sections_on_file") or count)
        status = str(course.get("status") or "").strip().upper()
        current = [
            str(value)
            for value in (
                course.get("expected_plan_sections")
                if expected_baseline
                else course.get("currently_registered_sections")
            )
            or []
        ]
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
                if status == "NOT_MATCHING_STUDENT_PROFILE" and recorded_count:
                    lines.append(
                        f"توجد للمقرر {code} شُعب مدرجة في بيانات النظام وعددها "
                        f"{recorded_count}، لكنها لا تطابق برنامجك أو شطر الدراسة "
                        "المسجّل في ملفك."
                    )
                else:
                    lines.append(f"لا تظهر للمقرر {code} أي شعبة مدرجة في بيانات النظام.")
                continue
            if current:
                joined = "، ".join(current)
                sentence = (
                    f"الشعبة {joined} للمقرر {code} مدرجة في الجدول المتوقع"
                    if expected_baseline
                    else f"الشعبة {joined} للمقرر {code} مدرجة في الجدول المسجّل فعليًا"
                )
                if term:
                    sentence += f" للفصل {term}"
                sentence += "."
                lines.append(sentence)
                if all(section in free for section in current):
                    lines.append("ولا يظهر لها تعارض عند مقارنتها ببقية الجدول المرجعي.")
            else:
                lines.append(f"عدد الشُعب المدرجة للمقرر {code} في بيانات النظام: {count}.")
            if clashing:
                label = "الجدول المتوقع" if expected_baseline else "الجدول المسجّل فعليًا"
                lines.append(f"الشُعب التي تتعارض مع {label}: " + "، ".join(clashing) + ".")
        else:
            if not count:
                if status == "NOT_MATCHING_STUDENT_PROFILE" and recorded_count:
                    lines.append(
                        f"The system records {recorded_count} sections for {code}, but none "
                        "match the programme or study cohort recorded in your profile."
                    )
                else:
                    lines.append(f"No section for {code} is recorded in this system's data.")
                continue
            if current:
                joined = ", ".join(current)
                sentence = (
                    f"Section {joined} of {code} is in your expected timetable"
                    if expected_baseline
                    else f"Section {joined} of {code} is already in your current timetable"
                )
                if term:
                    sentence += f" for {term}"
                sentence += "."
                lines.append(sentence)
                if all(section in free for section in current):
                    lines.append("It shows no clash when compared with the rest of your timetable.")
            else:
                lines.append(f"The system has {count} recorded sections for {code}.")
            if clashing:
                label = "expected timetable" if expected_baseline else "current timetable"
                lines.append(f"Sections that clash with your {label}: " + ", ".join(clashing) + ".")
    if not lines:
        return ""
    if expected_baseline:
        lines.append(
            "الجدول المتوقع مخصص للتخطيط، ولا يُعد تسجيلًا فعليًا في بوابة الجامعة."
            if language == "Arabic"
            else "This is an expected plan, not actual registration in the university portal."
        )
    lines.append(
        "يعتمد هذا الفحص على بيانات الشُعب المحفوظة في النظام، ولا يثبت توفر مقعد. "
        "لم يسجّل النظام أي شعبة ولم يغيّر التسجيل في بوابة الجامعة."
        if language == "Arabic"
        else "This is a timetable check only; no section was registered or changed in the university portal."
    )
    return _apply_saudi_register("\n".join(lines), language, answer_style)


def _comparison_status_text(status: Any, language: str) -> str:
    value = str(status or "unknown").strip().lower()
    if language == "Arabic":
        return {
            "passed": "مجتاز",
            "studying": "قيد الدراسة في سجلك",
            "open_now": "متطلباته السابقة مستوفاة",
            "blocked": "بعض متطلباته السابقة غير مستوفاة",
            "unknown": "تعذّر تحديد حالته من البيانات المتاحة",
        }.get(value, "تعذّر تحديد حالته من البيانات المتاحة")
    return {
        "passed": "already passed",
        "studying": "being studied now",
        "open_now": "recorded prerequisites satisfied",
        "blocked": "blocked by recorded prerequisites",
        "unknown": "status not determinable from the data",
    }.get(value, "status not determinable from the data")


def _safe_course_comparison_answer(
    language: str,
    tool_results: list[dict[str, Any]],
    answer_style: str = "",
) -> str:
    """Render the deterministic comparison without asking the model to score it."""
    result = next(
        (row for row in reversed(tool_results) if row.get("tool") == "course_choice_comparison"),
        None,
    )
    if not result:
        return ""
    if not result.get("ok"):
        answer = (
            "تعذّر إعداد مقارنة موثوقة بين المقررات المحددة. اذكر رموز مقررين إلى "
            "أربعة مقررات مختلفة، مثل: «هل أختار AI331 أم DS341؟»."
            if language == "Arabic"
            else (
                "I could not build a reliable comparison for those choices. Name two to "
                "four different exact course codes, for example: ‘AI331 or DS341?’"
            )
        )
        return _apply_saudi_register(answer, language, answer_style)

    candidates = [row for row in result.get("candidates") or [] if isinstance(row, dict)]
    verdict = str(result.get("verdict") or "NOT_DETERMINABLE").upper()
    preferred = str(result.get("preferred_course") or "").strip().upper()
    lines: list[str] = []
    if language == "Arabic":
        if verdict == "PREFERRED" and preferred:
            lines.append(
                f"**الخلاصة:** يتقدّم {preferred} وفق الهدف الذي حددته والبيانات التي أمكن التحقق منها."
            )
        elif verdict == "TIE":
            lines.append("**الخلاصة:** النتيجة متعادلة في الجوانب التي أمكن التحقق منها.")
        else:
            lines.append(
                "**الخلاصة:** لا تثبت البيانات أن أحد المقررات أفضل في جميع الجوانب؛ "
                "الاختيار يعتمد على أولويتك."
            )
    else:
        if verdict == "PREFERRED" and preferred:
            lines.append(
                f"**Conclusion:** {preferred} leads for your stated objective on the "
                "verified evidence."
            )
        elif verdict == "TIE":
            lines.append("**Conclusion:** The verified dimensions are tied.")
        else:
            lines.append(
                "**Conclusion:** The evidence does not establish one course as better on "
                "every dimension; the choice depends on your priority."
            )

    for row in candidates:
        code = str(row.get("course_code") or "").strip().upper()
        name = str(row.get("course_name") or "").strip()
        heading = f"**{code}**" + (f" — {name}" if name else "")
        parts: list[str] = [_comparison_status_text(row.get("academic_status"), language)]
        missing: list[str] = []
        for raw_missing in row.get("missing_prerequisites") or []:
            if isinstance(raw_missing, dict):
                missing_code = str(raw_missing.get("course_code") or "").strip()
                if missing_code:
                    missing.append(missing_code)
                    continue
                if str(raw_missing.get("kind") or "").upper() == "MISSING_HOURS":
                    required = raw_missing.get("required")
                    if required is not None:
                        missing.append(
                            f"يشترط إكمال {int(required)} ساعة معتمدة"
                            if language == "Arabic"
                            else f"{int(required)}-credit gate"
                        )
            elif str(raw_missing).strip():
                missing.append(str(raw_missing).strip())
        if missing:
            parts.append(
                ("المتطلبات غير المستوفاة: " if language == "Arabic" else "missing: ")
                + ", ".join(missing)
            )

        recommendation = row.get("recommendation") or {}
        rec_state = str(recommendation.get("state") or "").upper()
        rec_rank = recommendation.get("rank")
        if rec_state in {"RECOMMENDED", "NEW_RECOMMENDATION"}:
            parts.append(
                (f"ضمن توصية النظام (الترتيب {int(rec_rank)})" if rec_rank else "ضمن توصية النظام")
                if language == "Arabic"
                else (
                    f"system recommendation rank {int(rec_rank)}"
                    if rec_rank
                    else "system-recommended"
                )
            )
        elif rec_state in {"ALREADY_IN_CURRENT_TIMETABLE", "CURRENT_BASELINE"}:
            parts.append(
                "ضمن الجدول المسجّل فعليًا" if language == "Arabic" else "in the registered baseline"
            )
        elif rec_state in {"ALREADY_IN_EXPECTED_PLAN", "EXPECTED_BASELINE"}:
            parts.append(
                "ضمن الجدول المتوقع" if language == "Arabic" else "in the expected-plan baseline"
            )
        elif rec_state:
            parts.append(
                "ليس ضمن توصية النظام لهذه المقارنة"
                if language == "Arabic"
                else "not in the current system recommendation"
            )
        if rec_rank and rec_state in {
            "ALREADY_IN_CURRENT_TIMETABLE",
            "CURRENT_BASELINE",
            "ALREADY_IN_EXPECTED_PLAN",
            "EXPECTED_BASELINE",
        }:
            parts.append(
                f"ومدرج أيضًا في توصية النظام (الترتيب {int(rec_rank)})"
                if language == "Arabic"
                else f"also system recommendation rank {int(rec_rank)}"
            )

        impact = row.get("impact") or {}
        direct = int(impact.get("direct_unlock_count") or 0)
        chain = int(impact.get("chain_course_count") or 0)
        weighted = impact.get("weighted_downstream_score")
        parts.append(
            (
                "عدد المقررات التي تصبح متطلباتها السابقة مستوفاة مباشرةً بعد اجتيازه: "
                f"{direct}، وعدد المقررات المتبقية التي يدخل في سلسلة متطلباتها: {chain}"
            )
            if language == "Arabic"
            else f"directly unlocks {direct}; appears in a remaining chain of {chain}"
        )
        if weighted is not None:
            parts.append(
                f"مؤشر أثره في الخطة: {float(weighted):.2f}"
                if language == "Arabic"
                else f"plan-impact weight {float(weighted):.2f}"
            )

        timetable = row.get("timetable") or {}
        timetable_status = str(timetable.get("status") or "").upper()
        if timetable_status == "NOT_ON_FILE":
            parts.append(
                "لا تظهر له شُعب في بيانات النظام"
                if language == "Arabic"
                else "no section is recorded in this system's data"
            )
        elif timetable_status in {"OK", "ALL_CLASH"}:
            recorded = int(timetable.get("sections_on_file") or 0)
            free = int(timetable.get("clash_free_count") or 0)
            parts.append(
                f"عدد الشُعب المدرجة: {recorded}، وعدد الشُعب التي لا تتعارض "
                f"مواعيدها منفردةً مع الجدول المرجعي: {free}"
                if language == "Arabic"
                else f"{recorded} recorded section(s), {free} individually clash-free"
            )
        elif timetable_status == "NOT_DETERMINABLE":
            reason_code = str(timetable.get("reason_code") or "").upper()
            if reason_code == "BASELINE_MEETING_DATA_INCOMPLETE":
                parts.append(
                    "لا يمكن حسم التعارض لأن بيانات مواعيد الجدول المرجعي ناقصة أو غير صالحة"
                    if language == "Arabic"
                    else (
                        "timetable fit is not determinable because baseline meeting "
                        "data is incomplete or invalid"
                    )
                )
            elif reason_code == "CANDIDATE_MEETING_DATA_INCOMPLETE":
                parts.append(
                    "لا يمكن حسم التعارض لأن بيانات مواعيد إحدى الشعب المرشحة ناقصة أو غير صالحة"
                    if language == "Arabic"
                    else (
                        "timetable fit is not determinable because candidate-section "
                        "meeting data is incomplete or invalid"
                    )
                )
            elif reason_code == "SECTION_SNAPSHOT_TERM_MISMATCH":
                parts.append(
                    "لا يمكن حسم التعارض لأن بيانات الشُعب لا تخص فصل المقارنة المطلوب"
                    if language == "Arabic"
                    else (
                        "timetable fit is not determinable because the current section "
                        "snapshot does not belong to the requested comparison term"
                    )
                )
            else:
                parts.append(
                    "لا يمكن حسم ملاءمة الجدول من البيانات المتاحة"
                    if language == "Arabic"
                    else "timetable fit is not determinable from the recorded data"
                )

        graduation = row.get("graduation") or {}
        if (
            graduation.get("simulation_completed")
            and graduation.get("estimated_additional_terms") is not None
        ):
            terms = int(graduation["estimated_additional_terms"])
            parts.append(
                f"محاكاة إكمال الخطة مكتملة؛ عدد الفصول الإضافية المقدّر: {terms}"
                if language == "Arabic"
                else f"completed graduation scenario: {terms} additional term(s)"
            )
        elif graduation:
            lower = graduation.get("lower_bound_additional_terms")
            parts.append(
                (
                    "محاكاة إكمال الخطة غير مكتملة؛ الحد الأدنى المقدّر لعدد الفصول "
                    f"الإضافية: {int(lower)}"
                    if lower is not None
                    else "محاكاة إكمال الخطة غير مكتملة؛ لذلك لا يمكن تحديد فرق الفصول"
                )
                if language == "Arabic"
                else (
                    f"graduation scenario incomplete; lower bound {int(lower)} term(s)"
                    if lower is not None
                    else "graduation scenario incomplete; the term difference is not determinable"
                )
            )
        lines.append(heading + ": " + "؛ ".join(parts) + ".")

    if language == "Arabic":
        lines.append(
            "مؤشر أثر الخطة أداة تخطيط داخلية، وليس ترتيبًا رسميًا من الجامعة. "
            "ويعتمد فحص الشُعب على البيانات المحفوظة في النظام؛ فلا يثبت توفر "
            "مقاعد أو استيفاء جميع شروط التسجيل. هذه المقارنة للقراءة فقط، ولم "
            "يسجّل النظام أي مقرر أو يغيّره."
        )
    else:
        lines.append(
            "The plan-impact weight is this project's planning heuristic, not an official "
            "university ranking. Section checks use the catalogue recorded here and do not "
            "prove live seats or registration permission. This comparison is read-only; no "
            "course was registered or changed."
        )
    return _apply_saudi_register("\n\n".join(lines), language, answer_style)


def _replacement_timing_text(improvement: dict[str, Any], language: str) -> str:
    effect = str(improvement.get("timing_effect") or "").upper()
    saved = improvement.get("terms_saved")
    if effect == "EARLIER" and saved is not None:
        return (
            "تشير المحاكاة المكتملة إلى تقليص المدة المتبقية بما يعادل "
            f"{int(saved)} من الفصول الدراسية"
            if language == "Arabic"
            else f"the completed simulation estimates {int(saved)} term(s) saved"
        )
    if effect == "FORECAST_COMPLETED":
        return (
            "تمكنت محاكاة السيناريو من إكمال مسار الخطة، بينما تعذّر ذلك في المحاكاة المرجعية"
            if language == "Arabic"
            else "the scenario completed a graduation path the baseline simulation could not complete"
        )
    resolved = [str(code) for code in improvement.get("blockers_resolved") or [] if str(code)]
    improved = [str(code) for code in improvement.get("blockers_improved") or [] if str(code)]
    if resolved:
        return (
            "أظهرت المحاكاة تحسن المسار بعد زوال العوائق الآتية: " + "، ".join(resolved)
            if language == "Arabic"
            else "the simulation proved improvement by resolving: " + ", ".join(resolved)
        )
    if improved:
        return (
            "أظهرت المحاكاة تحسنًا جزئيًا في العوائق الآتية: " + "، ".join(improved)
            if language == "Arabic"
            else "the simulation proved improvement to blockers: " + ", ".join(improved)
        )
    return (
        "أظهرت محاكاة إكمال الخطة تحسنًا في المسار الأكاديمي الكامل"
        if language == "Arabic"
        else "the complete graduation simulation proved an academic improvement"
    )


def _safe_feasible_replacement_answer(
    language: str,
    tool_results: list[dict[str, Any]],
    answer_style: str = "",
) -> str:
    """Render only the service's two independent proofs and bounded negatives."""
    result = next(
        (
            row
            for row in reversed(tool_results)
            if row.get("tool") == "feasible_course_replacements"
        ),
        None,
    )
    if not result:
        return ""
    if not result.get("ok"):
        answer = (
            "تعذّر تشغيل فحص الاستبدال الموثوق؛ لذلك لن أقترح استبدالًا من دون "
            "دليل أكاديمي وجدول مكتمل بلا تعارضات. لم يتغيّر تسجيلك الفعلي ولا "
            "الجدول المسجّل فعليًا."
            if language == "Arabic"
            else (
                "I could not run the verified replacement check, so I will not suggest a "
                "swap without both academic and complete-timetable evidence. Your real "
                "registration and timetable were not changed."
            )
        )
        return _apply_saudi_register(answer, language, answer_style)

    baseline_kind = str(result.get("baseline_kind") or "").upper()
    if language == "Arabic":
        baseline_text = {
            "REGISTERED": "الجدول المسجّل فعليًا",
            "EXPECTED_PLAN": "الجدول المتوقع",
            "EMPTY": "عدم وجود مقررات في الجدول المرجعي",
            "MIXED_REVIEW_REQUIRED": "بيانات الجدولين التي تحتاج إلى مراجعة",
        }.get(baseline_kind, "الجدول المرجعي المحفوظ في النظام")
    else:
        baseline_text = {
            "REGISTERED": "your registered timetable",
            "EXPECTED_PLAN": "your expected-plan timetable",
            "EMPTY": "the empty planning baseline",
            "MIXED_REVIEW_REQUIRED": "mixed timetable data that needs review",
        }.get(baseline_kind, "the timetable baseline recorded in this system")

    certified = [row for row in result.get("certified_replacements") or [] if isinstance(row, dict)]
    lines: list[str] = []
    if certified:
        lines.append(
            "**الخلاصة:** عدد الاستبدالات التي ثبت تحسنها أكاديميًا وأمكن إنشاء "
            f"جدول مكتمل لها بلا تعارضات بالاستناد إلى {baseline_text}: {len(certified)}."
            if language == "Arabic"
            else (
                f"**Conclusion:** I found {len(certified)} academically proven replacement(s) "
                f"with a complete clash-free schedule around {baseline_text}."
            )
        )
        for index, row in enumerate(certified, start=1):
            removed = str((row.get("remove_course") or {}).get("course_code") or "").upper()
            added = str((row.get("add_course") or {}).get("course_code") or "").upper()
            improvement = row.get("academic_improvement") or {}
            timetable = row.get("timetable") or {}
            options = [
                option
                for option in timetable.get("certified_options") or []
                if isinstance(option, dict)
            ]
            first = options[0] if options else {}
            sections = [
                f"{section.get('course_code')} {section.get('section')}"
                for section in first.get("complete_sections") or []
                if section.get("course_code") and section.get("section")
            ]
            if language == "Arabic":
                lines.append(
                    f"{index}. **استبدال {removed} بالمقرر {added}**: "
                    + _replacement_timing_text(improvement, language)
                    + f". عدد الجداول المقترحة المكتملة التي تحقق منها النظام: {len(options)}"
                    + (f"؛ الشُعب في أول جدول: {', '.join(sections)}" if sections else "")
                    + "."
                )
                if row.get("outside_plan_addition"):
                    lines.append(
                        f"   تنبيه: {added} غير مدرج ضمن متطلبات الخطة المحفوظة. "
                        "الأثر الظاهر في المحاكاة لا يجعله بديلًا عن أحد متطلبات الخطة."
                    )
            else:
                lines.append(
                    f"{index}. **{removed} → {added}**: "
                    + _replacement_timing_text(improvement, language)
                    + f". The planner certified {len(options)} complete option(s)"
                    + (f"; the first uses {', '.join(sections)}" if sections else "")
                    + "."
                )
                if row.get("outside_plan_addition"):
                    lines.append(
                        f"   Note: {added} is outside the recorded degree-plan requirements; its simulated academic effect does not make it a substitute for a plan requirement."
                    )
    else:
        requested_remove = str(result.get("requested_remove_course") or "").upper()
        requested_add = str(result.get("requested_add_course") or "").upper()
        exact = (
            f"استبدال {requested_remove} بالمقرر {requested_add}"
            if requested_remove and requested_add
            else requested_remove or requested_add
        )
        rejections = [
            row for row in result.get("rejected_replacements") or [] if isinstance(row, dict)
        ]
        academic_rejected = any(
            str((row.get("academic") or {}).get("status") or "").upper()
            in {"ACADEMIC_INVALID", "ACADEMIC_NOT_IMPROVING"}
            for row in rejections
        )
        timetable_statuses = {
            str((row.get("timetable") or {}).get("status") or "").upper() for row in rejections
        }
        snapshot_term_mismatch = any(
            str((row.get("timetable") or {}).get("reason_code") or "").upper()
            == "SECTION_SNAPSHOT_TERM_MISMATCH"
            for row in rejections
        )
        if language == "Arabic":
            subject = f"بالنسبة إلى {exact}: " if exact else ""
            if academic_rejected:
                lines.append(
                    f"**الخلاصة:** {subject}لم تثبت المحاكاة تحسن مسار إكمال الخطة؛ "
                    "لذلك لم يعتمد النظام أي استبدال بوصفه أفضل، ولم تُستنتج منه "
                    "ملاءمة الجدول."
                )
            elif snapshot_term_mismatch:
                lines.append(
                    f"**الخلاصة:** {subject}بيانات الشُعب المحفوظة لا تخص الفصل المطلوب؛ "
                    "لذلك توقف الفحص، ولم يُعتمد جدول بلا تعارضات."
                )
            elif "NOT_DETERMINABLE" in timetable_statuses:
                lines.append(
                    f"**الخلاصة:** {subject}أمكن فحص الجانب الأكاديمي، لكن بيانات "
                    "الشُعب أو الجدول لم تكفِ لاعتماد جدول مكتمل بلا تعارضات."
                )
            else:
                lines.append(
                    f"**الخلاصة:** {subject}لم ينتج الفحص المحدود استبدالًا يجمع "
                    "بين تحسن مسار إكمال الخطة وإمكان إنشاء جدول مكتمل بلا تعارضات."
                )
            lines.append(
                "تقتصر هذه النتيجة على البيانات والخيارات التي فُحصت، ولا تثبت عدم "
                "وجود ترتيب آخر خارج نطاق البحث."
            )
        else:
            subject = f"for {exact}" if exact else "in the requested search"
            if academic_rejected:
                lines.append(
                    f"**Conclusion:** The graduation simulation did not prove an academic improvement {subject}, so I did not certify it as a better replacement or infer timetable feasibility."
                )
            elif snapshot_term_mismatch:
                lines.append(
                    f"**Conclusion:** The recorded section snapshot does not belong to the requested term {subject}, so I stopped before running the academic-improvement simulation and did not certify a clash-free timetable."
                )
            elif "NOT_DETERMINABLE" in timetable_statuses:
                lines.append(
                    f"**Conclusion:** Academic evidence was evaluable, but the recorded section or timetable data was insufficient to certify a complete clash-free schedule {subject}."
                )
            else:
                lines.append(
                    f"**Conclusion:** The bounded check did not produce a replacement that passed both the graduation-improvement and complete clash-free timetable gates {subject}."
                )
            lines.append(
                "This was a bounded search of recorded data, not proof that no other arrangement exists."
            )

    lines.append(
        "يعتمد فحص الجدول على بيانات الشُعب المحفوظة في النظام، ولا تتضمن هذه "
        "البيانات تأكيدًا لطرح الشعبة حاليًا أو لوجود مقعد شاغر أو لاستيفاء جميع "
        "شروط التسجيل. النتيجة للقراءة فقط، ولم يُحذف أو يُضف أو يُسجّل أي مقرر."
        if language == "Arabic"
        else (
            "The timetable proof uses the recorded section snapshot and deliberately "
            "ignores capacity; it does not prove a current offering, live seat, or "
            "registration permission. This is read-only: no course was dropped, added, "
            "or registered."
        )
    )
    return _apply_saudi_register("\n\n".join(lines), language, answer_style)


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
    r"جميع\s+(?:البدائل|الخيارات)\s+الممكنة|"
    r"ما\s+(?:فيه|في)\s+(?:أي\s+)?(?:خيار|ترتيب).*?"
    r"(?:يجمع|يحط|يستوعب).*?(?:كل|جميع)\s+(?:المواد|المقررات)))",
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
    r"(?:المواد|المقررات).*?مو\s+(?:متاحة|مطروحة|مفتوحة)|"
    r"ما\s+فيه\s+(?:مواد|مقررات).*?(?:متاحة|مطروحة|مفتوحة)|"
    r"وصلت\s+(?:الحد|السقف)|"
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

_PLANNING_BASELINE_CURRENT_TERM_CLAIM = re.compile(
    r"(?:\b(?<!not\s)(?:after|including|in|during|for|through)\s+"
    r"(?:(?:your|the|this)\s+)?current\s+(?:one|term|semester)\b|"
    r"\bcurrent\s+planning\s+baseline\b|"
    r"(?:بعد|شامل(?:ة)?[\u064b-\u0652]*ا?|باحتساب|بما\s+(?:فيه|فيها|يشمل)|خلال|في|من)\s+"
    r"(?:الفصل\s+الحالي|فصل(?:ي|ك|ه|ها|هم)?\s+الحالي|"
    r"الترم\s+الحالي|ترم(?:ي|ك|ه|ها|هم)?\s+الحالي|"
    r"هالترم|هالفصل|(?:الترم|الفصل)\s+(?:ذا|هذا))|"
    r"(?:الأساس\s+التخطيطي|الفصل\s+المرجعي\s+للتخطيط)\s+الحالي|"
    r"(?:\|\s*(?:\*{0,2}current\*{0,2}|\*{0,2}الحالي\*{0,2})\s*\||"
    r"(?:term|semester|الفصل|فصل|الترم)\s*:\s*"
    r"(?:\*{0,2}current\*{0,2}|\*{0,2}الحالي\*{0,2})))",
    re.IGNORECASE,
)

_PLANNING_BASELINE_CURRENT_TERM_CONTRAST = re.compile(
    r"(?:\b(?:not(?:\s+(?:in|for|during))?|different\s+from|differs\s+from|"
    r"rather\s+than|unlike)\s+(?:(?:your|the|this)\s+)?current\s+(?:term|semester)\b|"
    r"(?:ليس(?:ت)?(?:\s+(?:هو|هي))?(?:\s+(?:في|ضمن))?|يختلف\s+عن|"
    r"مختلف(?:ة)?\s+عن|بدل(?:ًا|ا)?\s+من)\s+"
    r"(?:الفصل\s+الدراسي\s+الحالي|الفصل\s+الحالي|"
    r"فصل(?:ي|ك|ه|ها|هم)\s+الحالي|الترم\s+الحالي|"
    r"ترم(?:ي|ك|ه|ها|هم)\s+الحالي))",
    re.IGNORECASE,
)

_PLANNING_BASELINE_CURRENT_TERM_EQUIVALENCE = re.compile(
    r"(?:\b(?:planning\s+baseline|baseline\s+term|forecast\s+baseline)\s+"
    r"(?:is|equals|matches)\s+(?:the\s+same\s+as\s+)?"
    r"(?:(?:your|the|this)\s+)?current\s+(?:term|semester)\b|"
    r"(?:الفصل\s+المرجعي\s+للتخطيط|الفصل\s+المرجعي|أساس\s+التخطيط)\s+"
    r"(?:هو|يساوي|يطابق|نفس)\s+(?:نفس\s+)?"
    r"(?:الفصل\s+الدراسي\s+الحالي|الفصل\s+الحالي|"
    r"فصل(?:ي|ك|ه|ها|هم)\s+الحالي|الترم\s+الحالي|"
    r"ترم(?:ي|ك|ه|ها|هم)\s+الحالي))",
    re.IGNORECASE,
)

_PLANNING_BASELINE_CURRENT_TERM_ASSIGNMENT = re.compile(
    r"(?:\b(?:simulation|scenario|forecast|plan)\s+"
    r"(?:treats|labels|identifies|calls|sets)\s+"
    r"(?:(?:your|the|this)\s+)?current\s+(?:term|semester)\s+as\s+|"
    r"(?:المحاكاة|السيناريو|التوقع|الخطة)\s+"
    r"(?:تعتبر|تسمي|تحدد)\s+"
    r"(?:الفصل\s+الدراسي\s+الحالي|الفصل\s+الحالي|"
    r"فصل(?:ي|ك|ه|ها|هم)\s+الحالي|الترم\s+الحالي|"
    r"ترم(?:ي|ك|ه|ها|هم)\s+الحالي)\s+(?:هو|كـ?))",
    re.IGNORECASE,
)


def _mislabels_planning_baseline_as_current(answer: str, graduation: dict[str, Any] | None) -> bool:
    """Whether prose promotes a simulation baseline to current-term evidence.

    The selected Planner baseline may be a manually seeded expected timetable.
    Graduation output intentionally does not claim that it is the registrar's
    current term, so student-facing prose must use the neutral planning-baseline
    label even when the numeric year/term happens to match today's configuration.
    """
    if not graduation:
        return False
    year = graduation.get("planning_baseline_academic_year")
    term = graduation.get("planning_baseline_term")
    if year is None or term is None:
        return False
    candidate = _PLANNING_BASELINE_CURRENT_TERM_CONTRAST.sub("", answer or "")
    target = rf"{re.escape(str(year))}\s*/\s*{re.escape(str(term))}"
    if re.search(
        rf"{_PLANNING_BASELINE_CURRENT_TERM_ASSIGNMENT.pattern}\s*{target}\b",
        candidate,
        re.IGNORECASE,
    ):
        return True
    if _PLANNING_BASELINE_CURRENT_TERM_EQUIVALENCE.search(candidate):
        return True
    if _PLANNING_BASELINE_CURRENT_TERM_CLAIM.search(candidate):
        return True

    # Catch direct/table labels only when they are explicitly bound to this
    # scenario's verified planning-baseline term. This avoids broad matches such
    # as “different from your current term” while covering common Markdown prose.
    current_label = (
        r"(?:current(?:\s+(?:term|semester))?|"
        r"(?:your|the|this)\s+current\s+(?:term|semester)|"
        r"الفصل\s+الدراسي\s+الحالي|الفصل\s+الحالي|"
        r"فصل(?:ي|ك|ه|ها|هم)\s+الحالي|الترم\s+الحالي|"
        r"ترم(?:ي|ك|ه|ها|هم)\s+الحالي|الحالي)"
    )
    direct_label = re.compile(
        rf"(?im)(?:^|[,.،؛;:]\s+|\b(?:scenario|plan|forecast)\s*,?\s+)"
        rf"(?:[-*]\s*)?\*{{0,2}}{current_label}\*{{0,2}}\s*"
        rf"(?:\||[-–—:]|\b(?:is|هو)\b|\()?\s*{target}\s*\)?\b"
    )
    return bool(direct_label.search(candidate))


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
    baseline_mislabeled = _mislabels_planning_baseline_as_current(candidate, graduation)
    if completed:
        required_numbers = [graduation.get("estimated_additional_terms")]
        if graduation.get("planning_baseline_courses_assumed_passed"):
            required_numbers.append(graduation.get("estimated_terms_including_planning_baseline"))
        missing_numbers = [
            int(number)
            for number in required_numbers
            if number is not None and str(int(number)) not in candidate
        ]
        if not missing_numbers and not baseline_mislabeled:
            return None
        return {
            "simulation_completed": True,
            "estimated_additional_terms": graduation.get("estimated_additional_terms"),
            "estimated_terms_including_planning_baseline": graduation.get(
                "estimated_terms_including_planning_baseline"
            ),
            "missing_numbers": missing_numbers,
            "mislabels_planning_baseline_as_current": baseline_mislabeled,
        }

    unresolved = [
        row
        for row in graduation.get("unresolved_requirements") or []
        if isinstance(row, dict) and str(row.get("code") or "").strip()
    ]
    required_numbers = [graduation.get("lower_bound_additional_terms")]
    if graduation.get("planning_baseline_courses_assumed_passed"):
        required_numbers.append(graduation.get("lower_bound_terms_including_planning_baseline"))
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
        and not baseline_mislabeled
    ):
        return None
    return {
        "simulation_completed": False,
        "lower_bound_additional_terms": graduation.get("lower_bound_additional_terms"),
        "lower_bound_terms_including_planning_baseline": graduation.get(
            "lower_bound_terms_including_planning_baseline"
        ),
        "unresolved_requirements": unresolved,
        "missing_numbers": missing_numbers,
        "missing_codes": missing_codes,
        "missing_blocker_details": missing_blocker_details,
        "must_state_lower_bound": not bool(_LOWER_BOUND_MARKERS.search(candidate)),
        "mislabels_planning_baseline_as_current": baseline_mislabeled,
    }


_GRADUATION_UNSUPPORTED_INFERENCE = re.compile(
    r"(?:\b(?:may|might|could)\s+(?:require|need|add).*?\b(?:extra|additional)\s+term\b|"
    r"\bspecial\s+arrangement\b|\bno\s+available\s+(?:time|section|offering)\b|"
    r"\b(?:maximum\s+allowed|permitted\s+maximum)\b|"
    r"(?:قد|مما)\s+(?:يستدعي|يتطلب).*?فصل(?:اً|ًا|ا)?\s+إضاف|"
    r"ترتيب(?:اً|ًا|ا)?\s+خاص|"
    r"الحد\s+الأقصى\s+المسموح|"
    r"(?:لا|لم)\s+(?:يوجد|يظهر).*?(?:موعد|مكان|شعبة).*?(?:متاح|الفصول|المحاك)|"
    r"(?:يمكن|ممكن|شكله).*?(?:يحتاج|يبغى\s+له).*?ترم\s+(?:زيادة|إضافي)|"
    r"ما\s+(?:فيه|له).*?(?:وقت|مكان|شعبة).*?(?:متاح|مناسب|مطروح))",
    re.IGNORECASE | re.DOTALL,
)


def _what_if_error_text(error: dict[str, Any], language: str) -> str:
    kind = str(error.get("kind") or "")
    code = str(error.get("course_code") or "").strip()
    if language == "Arabic":
        messages = {
            "NOT_IN_CURRENT_TIMETABLE": (
                f"{code} ليس ضمن المقررات المرجعية المستخدمة في المحاكاة."
            ),
            "ALREADY_IN_CURRENT_TIMETABLE": (
                f"{code} مدرج بالفعل ضمن المقررات المرجعية المستخدمة في المحاكاة."
            ),
            "ALREADY_PASSED": f"تظهر حالة {code} في السجل الأكاديمي على أنها «مجتاز».",
            "COURSE_NOT_ON_FILE": f"لا توجد بيانات مقرر موثوقة للرمز {code}.",
            "COURSE_CREDITS_UNKNOWN": f"عدد الساعات المعتمدة للمقرر {code} غير معروف.",
            "ELECTIVE_PLACEHOLDER_NOT_A_COURSE": (
                f"{code} رمز لمتطلب اختياري، وليس رمزًا لمقرر محدد."
            ),
            "SAME_COURSE_REMOVED_AND_ADDED": "لا يمكن حذف المقرر نفسه وإضافته في السيناريو ذاته.",
            "SEARCH_CANNOT_BE_COMBINED_WITH_EXPLICIT_CHANGES": (
                "لا يمكن الجمع بين البحث التلقائي وتحديد تغييرات بعينها في الطلب نفسه."
            ),
            "TOO_MANY_CHANGES": "عدد التغييرات المطلوبة يتجاوز الحد الذي تسمح به المحاكاة.",
        }
        if kind == "SCENARIO_EXCEEDS_CREDIT_CAP":
            return (
                f"السيناريو يصل إلى {int(error.get('credits') or 0)} ساعة، ويتجاوز "
                f"حد المحاكاة {int(error.get('maximum') or 0)} ساعة."
            )
        if kind == "ADDED_COURSE_PREREQUISITES_UNMET":
            missing = "، ".join(error.get("missing_prerequisites") or [])
            return f"المتطلبات السابقة المدرجة للمقرر {code} غير مستوفاة: {missing}."
        if kind == "ADDED_COURSE_CREDIT_GATE_UNMET":
            return (
                f"شرط الساعات للمقرر {code} غير مستوفى: المطلوب "
                f"{int(error.get('required') or 0)} ساعة معتمدة، بينما يحتسب السيناريو "
                f"{int(error.get('effective') or 0)} ساعة."
            )
        return messages.get(kind, "تعذّر التحقق من التغيير المطلوب في مقررات المحاكاة.")

    messages = {
        "NOT_IN_CURRENT_TIMETABLE": f"{code} is not in the planning-baseline timetable.",
        "ALREADY_IN_CURRENT_TIMETABLE": f"{code} is already in the planning baseline.",
        "ALREADY_PASSED": f"{code} is recorded as passed.",
        "COURSE_NOT_ON_FILE": f"No reliable course record was found for {code}.",
        "COURSE_CREDITS_UNKNOWN": f"The credit value for {code} is unknown.",
        "ELECTIVE_PLACEHOLDER_NOT_A_COURSE": f"{code} is an elective slot, not a concrete course.",
        "SAME_COURSE_REMOVED_AND_ADDED": "The same course cannot be removed and added in one scenario.",
        "SEARCH_CANNOT_BE_COMBINED_WITH_EXPLICIT_CHANGES": "Automatic replacement search cannot be combined with explicit changes.",
        "TOO_MANY_CHANGES": "The planning-baseline scenario contains too many changes.",
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
    return messages.get(kind, "The requested planning-baseline change could not be validated.")


def _comparison_effect_text(comparison: dict[str, Any], language: str) -> str:
    effect = str(comparison.get("timing_effect") or "NOT_DETERMINABLE")
    saved = int(comparison.get("terms_saved") or 0)
    delta = comparison.get("term_difference")
    if language == "Arabic":
        if effect == "EARLIER":
            return f"تشير المحاكاة إلى تقليص المدة المتبقية بما يعادل {saved} من الفصول الدراسية."
        if effect == "LATER":
            return (
                "تشير المحاكاة إلى زيادة المدة المتبقية بما يعادل "
                f"{abs(int(delta or 0))} من الفصول الدراسية."
            )
        if effect == "SAME":
            return "لم يتغيّر العدد المقدّر للفصول الدراسية بين السيناريوهين."
        if effect == "FORECAST_COMPLETED":
            return "أزال التغيير العوائق التي كانت تمنع اكتمال المحاكاة."
        if effect == "FORECAST_BECAME_UNRESOLVED":
            return "أصبحت مدة الإكمال غير قابلة للتقدير بعد أن كانت المحاكاة مكتملة."
        if effect == "UNRESOLVED_IMPROVEMENT":
            return "خفّف التغيير بعض العوائق المسجلة، لكنه لا يثبت إكمال الخطة في وقت أبكر."
        if effect == "UNRESOLVED_WORSE":
            return "أضاف التغيير عوائق غير محسومة؛ ولذلك كانت نتيجته الأكاديمية أضعف في المحاكاة."
        return "لا تكفي البيانات المتاحة لتحديد أثر دقيق في المدة المتبقية لإكمال الخطة."

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
            parts.append("العوائق التي زالت: " + "، ".join(resolved) + ".")
        if improved:
            parts.append("العوائق التي تحسنت جزئيًا: " + "، ".join(improved) + ".")
    else:
        if resolved:
            parts.append("Resolves: " + ", ".join(resolved) + ".")
        if improved:
            parts.append("Improves without fully resolving: " + ", ".join(improved) + ".")
    return " ".join(parts)


def _safe_graduation_what_if_answer_base(language: str, what_if: dict[str, Any]) -> str:
    if not what_if.get("valid"):
        errors = [
            _what_if_error_text(error, language)
            for error in what_if.get("validation_errors") or []
            if isinstance(error, dict)
        ]
        if language == "Arabic":
            return (
                "لم تُشغّل المحاكاة لأن التغيير المطلوب لم يجتز التحقق:\n- "
                + "\n- ".join(errors or ["تعذر التحقق من التغيير المطلوب."])
                + "\nلم يتغيّر السجل الأكاديمي أو الجدول المسجّل فعليًا."
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
                    "لم يثبت البحث الأكاديمي المحدود وجود استبدال مباشر يحسّن تقدير "
                    "التخرج مقارنة بالمقررات المرجعية المستخدمة في المحاكاة. ولا تثبت "
                    "هذه النتيجة استحالة وجود ترتيب آخر. "
                )
                if partial_count:
                    answer += (
                        f"استبعد البحث {partial_count} استبدالًا حسّن عائقًا جزئيًا فقط، "
                        "لأن المسار الكامل لإكمال الخطة لم يُظهر تحسنًا. "
                    )
                return answer + "لم يتغيّر أي مقرر أو الجدول المسجّل فعليًا."
            lines = ["الاستبدالات التي أظهرت المحاكاة أنها تحسّن المسار الأكاديمي:"]
            for row in replacements:
                removed = str((row.get("remove_course") or {}).get("code") or "")
                added = str((row.get("add_course") or {}).get("code") or "")
                comparison = row.get("comparison") or {}
                lines.append(
                    f"- استبدال {removed} بالمقرر {added}: "
                    + _comparison_effect_text(comparison, language)
                    + " "
                    + _comparison_blocker_text(comparison, language)
                )
            lines.append(
                "هذه مقارنة أكاديمية فقط. يجب فحص الشُعب والتعارضات في أداة الجدول؛ "
                "فالنتيجة لا تثبت توفر مقعد أو استيفاء جميع شروط التسجيل. لم يتغيّر "
                "الجدول المسجّل فعليًا."
            )
            return "\n".join(lines)
        if not replacements:
            answer = (
                "The bounded academic search found no one-for-one replacement proven to "
                "improve the graduation forecast over the planning-baseline timetable. This does not "
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
        lines = ["التغيير المفترض في مقررات المحاكاة: " + " و".join(change) + "."]
        lines.append(_comparison_effect_text(comparison, language))
        lines.append(
            "الحد الأدنى المقدّر لعدد الفصول الإضافية: "
            f"{baseline.get('lower_bound_additional_terms')} قبل التغيير، مقابل "
            f"{scenario.get('lower_bound_additional_terms')} بعده."
        )
        if resolved:
            lines.append("عوائق زالت في المحاكاة: " + "، ".join(resolved) + ".")
        if improved:
            lines.append("عوائق تحسنت ولم تُحسم بالكامل: " + "، ".join(improved) + ".")
        if introduced:
            lines.append("عوائق جديدة: " + "، ".join(introduced) + ".")
        if outside:
            lines.append(
                "المقررات "
                + "، ".join(outside)
                + " غير مدرجة ضمن متطلبات الخطة؛ وقد تؤثر في المتطلبات السابقة أو "
                "الساعات المكتسبة، لكنها لا تحل محل مقررات الخطة."
            )
        lines.append(
            "هذا سيناريو أكاديمي افتراضي للقراءة فقط. لم يُحذف أو يُضف أو يُسجّل "
            "أي مقرر فعليًا، ولا يثبت السيناريو وجود شعبة مطروحة أو مقعد شاغر أو "
            "جدول بلا تعارضات."
        )
        return "\n".join(lines)

    change = []
    if removed:
        change.append("remove " + ", ".join(removed))
    if added:
        change.append("add " + ", ".join(added))
    lines = ["Planning-baseline scenario: " + " and ".join(change) + "."]
    lines.append(_comparison_effect_text(comparison, language))
    lines.append(
        f"Additional-term lower bound: {baseline.get('lower_bound_additional_terms')} for "
        f"the baseline timetable versus {scenario.get('lower_bound_additional_terms')} for the scenario."
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


def _safe_graduation_what_if_answer(
    language: str,
    what_if: dict[str, Any],
    answer_style: str = "",
) -> str:
    return _apply_saudi_register(
        _safe_graduation_what_if_answer_base(language, what_if),
        language,
        answer_style,
    )


def _safe_graduation_answer(
    language: str,
    tool_results: list[dict[str, Any]],
    answer_style: str = "",
) -> str:
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
        return _safe_graduation_what_if_answer(language, what_if, answer_style)

    has_baseline = bool(graduation.get("planning_baseline_courses_assumed_passed"))
    baseline_year = graduation.get("planning_baseline_academic_year")
    baseline_term = graduation.get("planning_baseline_term")
    baseline_reference = (
        f" ({int(baseline_year)}/{int(baseline_term)})"
        if baseline_year is not None and baseline_term is not None
        else ""
    )
    baseline_codes = [
        str(course.get("code") or "").strip()
        for course in graduation.get("planning_baseline_courses_assumed_passed") or []
        if isinstance(course, dict) and str(course.get("code") or "").strip()
    ]
    cap = int(graduation.get("max_credits_per_term") or 18)
    unresolved = [
        row
        for row in graduation.get("unresolved_requirements") or []
        if isinstance(row, dict) and str(row.get("code") or "").strip()
    ]

    if language == "Arabic":
        if graduation.get("simulation_completed"):
            additional = int(graduation.get("estimated_additional_terms") or 0)
            opening = f"عدد الفصول الدراسية الإضافية التي تقدّرها المحاكاة: {additional}"
            if has_baseline:
                opening += (
                    ". والإجمالي باحتساب فصل المقررات المرجعية المستخدمة في المحاكاة"
                    f"{baseline_reference}: "
                    f"{int(graduation.get('estimated_terms_including_planning_baseline') or additional)}"
                )
            opening += "."
        else:
            additional = int(graduation.get("lower_bound_additional_terms") or 0)
            opening = f"الحد الأدنى المقدّر لعدد الفصول الدراسية الإضافية: {additional}"
            if has_baseline:
                opening += (
                    ". والحد الأدنى الإجمالي باحتساب فصل المقررات المرجعية المستخدمة "
                    f"في المحاكاة{baseline_reference}: "
                    f"{int(graduation.get('lower_bound_terms_including_planning_baseline') or additional)}"
                )
            opening += ". لا يمكن تحديد فصل الإكمال بدقة لأن بعض متطلبات الخطة لم تُحسم في المحاكاة."

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
                reasons.append("المتطلبات السابقة غير المستوفاة: " + "، ".join(prereqs))
            gate = row.get("credit_hour_gate")
            if isinstance(gate, dict):
                reasons.append(
                    f"شرط الساعات المعتمدة: {int(gate.get('required') or 0)}؛ "
                    f"الساعات المحتسبة في السيناريو: "
                    f"{int(gate.get('effective_in_scenario') or 0)}؛ "
                    f"الساعات المتبقية لاستيفاء الشرط: {int(gate.get('remaining') or 0)}"
                )
            blocker_lines.append(
                f"- {code}: " + ("؛ ".join(reasons) or "تعذّر حسم المتطلب من البيانات")
            )
        blockers = "\n" + "\n".join(blocker_lines) if blocker_lines else ""
        term_lines = []
        for planned in graduation.get("term_plan") or []:
            if not isinstance(planned, dict):
                continue
            year = planned.get("academic_year")
            term = planned.get("term")
            if year is None or term is None:
                continue
            codes = [
                str(code).strip() for code in planned.get("course_codes") or [] if str(code).strip()
            ]
            term_lines.append(
                f"- {int(year)}/{int(term)}: "
                + ("، ".join(codes) if codes else "لا توجد مقررات في هذا الفصل ضمن المحاكاة")
                + f" (إجمالي الساعات: {int(planned.get('credits') or 0)})"
            )
        term_plan = (
            "\nالتسلسل الفصلي الناتج عن المحاكاة:\n" + "\n".join(term_lines) if term_lines else ""
        )
        baseline_assumption = (
            " ويفترض اجتياز المقررات المرجعية الآتية: " + "، ".join(baseline_codes) + "."
            if baseline_codes
            else ""
        )
        return _apply_saudi_register(
            opening
            + blockers
            + term_plan
            + f"\nهذه محاكاة للقراءة فقط، وسقفها التخطيطي {cap} ساعة معتمدة لكل فصل رئيس،"
            + baseline_assumption
            + " كما تفترض اجتياز جميع المقررات من المحاولة الأولى. ولا تضمن طرح "
            "المقررات مستقبلًا أو وجود مقاعد شاغرة أو أوقات الشُعب أو استيفاء جميع "
            "شروط التسجيل، ولا تغيّر السجل الأكاديمي أو تسجّل أي مقرر.",
            language,
            answer_style,
        )

    if graduation.get("simulation_completed"):
        additional = int(graduation.get("estimated_additional_terms") or 0)
        opening = f"The scenario estimates {additional} additional terms"
        if has_baseline:
            opening += (
                ", or "
                f"{int(graduation.get('estimated_terms_including_planning_baseline') or additional)} "
                f"terms including the planning baseline{baseline_reference}"
            )
        opening += "."
    else:
        additional = int(graduation.get("lower_bound_additional_terms") or 0)
        opening = f"The lower bound is {additional} additional terms"
        if has_baseline:
            opening += (
                ", or "
                f"{int(graduation.get('lower_bound_terms_including_planning_baseline') or additional)} "
                f"terms including the planning baseline{baseline_reference}"
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
    term_lines = []
    for planned in graduation.get("term_plan") or []:
        if not isinstance(planned, dict):
            continue
        year = planned.get("academic_year")
        term = planned.get("term")
        if year is None or term is None:
            continue
        codes = [
            str(code).strip() for code in planned.get("course_codes") or [] if str(code).strip()
        ]
        term_lines.append(
            f"- {int(year)}/{int(term)}: "
            + (", ".join(codes) if codes else "waiting term in this simulation")
            + f" ({int(planned.get('credits') or 0)} credits)"
        )
    term_plan = "\nProjected terms:\n" + "\n".join(term_lines) if term_lines else ""
    baseline_assumption = (
        " It assumes these planning-baseline courses pass: " + ", ".join(baseline_codes) + "."
        if baseline_codes
        else ""
    )
    return (
        opening
        + blockers
        + term_plan
        + f"\nThis read-only scenario caps each main term at {cap} credits and assumes every "
        "course is passed on the first attempt."
        + baseline_assumption
        + " It cannot guarantee future offerings, seats, "
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
    r"(?:المصدر|الدليل|النص)\s+ما\s+(?:وضح|وضّح|ذكر|حدد|حسم|فصل)|مو\s+واضح|"
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
        if name in {"course_choice_comparison", "feasible_course_replacements"}:
            # These evidence checks are bound to trusted local term context; the
            # model cannot choose a different timetable baseline.
            properties.pop("academic_year", None)
            properties.pop("term", None)
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
    if name in {"course_choice_comparison", "feasible_course_replacements"}:
        # Defense in depth for callers that bypass schema-guided generation.
        arguments.pop("academic_year", None)
        arguments.pop("term", None)
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


def _citation_refusal(language: str, answer_style: str = "") -> str:
    if language == "Arabic":
        return _apply_saudi_register(
            "تعذّر التحقق من مرجع الإجابة في الأدلة المعتمدة؛ لذلك لن أعرض قاعدة "
            "غير موثقة. راجع مرشدك الأكاديمي أو عمادة القبول والتسجيل للتحقق من "
            "الحكم الرسمي.",
            language,
            answer_style,
        )
    return (
        "I could not verify the source for this answer against the approved records, "
        "so I will not present an unsupported rule. Please check with your academic "
        "adviser or the Deanship of Admission and Registration."
    )


def _claims_portal_action(answer: str) -> bool:
    return any(pattern.search(answer or "") for pattern in _PORTAL_ACTION_CLAIMS)


def _portal_boundary_response(language: str, answer_style: str = "") -> str:
    if language == "Arabic":
        answer = (
            "أستطيع إعداد جدول مقترح ومراجعته معك هنا، لكن هذا النظام لا يسجّل "
            "المقررات ولا يحفظ الجدول المقترح أو يطبقه في بوابة الجامعة. إذا اخترت "
            "أحد الجداول المقترحة، فأدخل مقرراته وشُعبه بنفسك في بوابة الجامعة "
            "الرئيسية."
        )
        return answer
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
    channel_profile: str = "",
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
    comparison_tool_context = {
        **tool_context,
        "section_snapshot_academic_year": int(academic_year),
        "section_snapshot_term": int(term),
    }
    replacement_tool_context = dict(comparison_tool_context)
    explicit_comparison_term = _explicit_comparison_year_term(clean_question)
    if explicit_comparison_term is not None:
        comparison_tool_context.update(
            academic_year=explicit_comparison_term[0],
            term=explicit_comparison_term[1],
        )

    language = _answer_language(clean_question)
    # Colloquial Saudi Arabic remains accepted and fully parsed as input, but the
    # student portal renders one consistent university register: clear MSA.
    detected_answer_style = _answer_style(clean_question)
    answer_style = (
        "Formal Modern Standard Arabic for a Saudi academic context"
        if language == "Arabic"
        else detected_answer_style
    )
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
    projected_policy = project_channel_tool_result(
        "policy_lookup",
        boundary.project_tool_result("policy_lookup", policy_result),
        profile=channel_profile,
    )
    policy_prompt = "\nverified_policy_evidence: " + json.dumps(
        _policy_evidence_for_prompt(projected_policy),
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )

    schemas = project_channel_tool_schemas(
        boundary.tool_schemas(student_v2_tool_schemas()),
        profile=channel_profile,
    )
    advertised = {str((schema.get("function") or {}).get("name") or "") for schema in schemas}
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": channel_system_prompt(SYSTEM_PROMPT, profile=channel_profile),
        },
        *_sanitize_history(project_channel_history(history, profile=channel_profile)),
        {
            "role": "user",
            "content": (
                f"answer_language: {language}\n"
                f"answer_style: {answer_style}\n"
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
    requires_feasible_replacement = _requires_feasible_course_replacements(clean_question)
    requires_course_comparison = (
        _requires_course_choice_comparison(clean_question) and not requires_feasible_replacement
    )
    requires_timetable_proposal = (
        _requires_timetable_proposal(clean_question)
        and not requires_course_comparison
        and not requires_feasible_replacement
    )
    # A proposal containing an exact pin is itself the authoritative clash/section
    # check for that build. Keep the independent section capability for inspection
    # questions only; otherwise the same sentence is interpreted twice and the
    # second call can contradict or obscure the hard pin.
    requires_section_check = (
        _requires_section_check(clean_question)
        and not requires_timetable_proposal
        and not requires_course_comparison
        and not requires_feasible_replacement
    )
    requires_graduation_progress = (
        _requires_graduation_progress(clean_question)
        and not requires_course_comparison
        and not requires_feasible_replacement
    )
    requires_graduation_what_if = (
        _requires_graduation_what_if(clean_question)
        and not requires_course_comparison
        and not requires_feasible_replacement
    )
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
    comparison_tool_reprompted = False
    comparison_safe_fallback_used = False
    replacement_tool_reprompted = False
    replacement_safe_fallback_used = False
    policy_uncertainty_reprompted = False
    internal_output_reprompted = False
    internal_output_sanitized = False
    constraint_input_refused = False

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
            has_course_comparison_evidence = any(
                row.get("tool") == "course_choice_comparison" and row.get("ok")
                for row in local_results
            )
            has_feasible_replacement_evidence = any(
                row.get("tool") == "feasible_course_replacements" and row.get("ok")
                for row in local_results
            )
            if (
                candidate
                and requires_feasible_replacement
                and not has_feasible_replacement_evidence
                and not replacement_tool_reprompted
            ):
                messages.append(turn.assistant_message)
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "This replacement request requires two fresh, independent proofs: "
                            "an improved complete graduation forecast and a complete clash-free "
                            "timetable retaining every other baseline section. Call "
                            "feasible_course_replacements now. Pass remove_course and/or "
                            "add_course only when that side is explicitly named in "
                            "student_question; leave an unstated side absent for the "
                            "deterministic search. Do not answer from an individual section "
                            "check, an academic-only scenario, or conversation history."
                        ),
                    }
                )
                replacement_tool_reprompted = True
                continue
            if (
                candidate
                and requires_course_comparison
                and not has_course_comparison_evidence
                and not comparison_tool_reprompted
            ):
                messages.append(turn.assistant_message)
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "This question explicitly compares named courses. Call "
                            "course_choice_comparison now with every exact course code from "
                            "student_question. Use one objective only: graduation, "
                            "unlock_impact, timetable_fit, or balanced. Do not answer from "
                            "history, add the dimensions into a made-up score, or choose a "
                            "winner without this fresh comparison evidence."
                        ),
                    }
                )
                comparison_tool_reprompted = True
                continue
            if (
                candidate
                and requires_section_check
                and not requires_timetable_proposal
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
                            "The student asked for a planning-baseline graduation what-if, not "
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
                            "Your draft conflicts with the verified section status or diverts "
                            "to policy-guide commentary. Use status and "
                            "recorded_sections_on_file exactly. When status is "
                            "NOT_MATCHING_STUDENT_PROFILE, say that sections are recorded but "
                            "none match the student's programme and study cohort. State any "
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
                            "estimate including the planning baseline. If it is incomplete, "
                            "do not invent an exact completion term: state the lower bound "
                            "both excluding and including the planning baseline, then name every "
                            "unresolved requirement and its returned prerequisite or "
                            "credit-hour blocker. Keep the assumptions (first-attempt passes, "
                            "18-credit main-term cap, and no guarantee of future offerings, "
                            "seats, or registration permission). Describe only those returned "
                            "facts. The planning baseline may be an expected timetable: call it "
                            "the planning baseline and never the student's actual current term. "
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
            comparison_normalizations: list[str] = []
            replacement_normalizations: list[str] = []
            constraint_input_error = ""
            if call.name == "graduation_progress":
                effective_arguments, scenario_normalization = _normalise_graduation_scenario_args(
                    clean_question,
                    model_arguments,
                )
            elif call.name == "course_choice_comparison":
                effective_arguments, comparison_normalizations = _normalise_course_comparison_args(
                    clean_question, model_arguments
                )
            elif call.name == "feasible_course_replacements":
                effective_arguments, replacement_normalizations = (
                    _normalise_feasible_replacement_args(clean_question, model_arguments)
                )
            elif call.name == "build_timetable_proposal":
                effective_arguments, timetable_normalizations = _normalise_timetable_proposal_args(
                    clean_question,
                    model_arguments,
                )
                # Private control signal: never advertise it and never let it
                # cross the remote boundary or reach the capability executor.
                constraint_input_error = str(
                    effective_arguments.pop("_constraint_input_error", "") or ""
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
                    **(
                        {"comparison_normalizations": comparison_normalizations}
                        if comparison_normalizations
                        else {}
                    ),
                    **(
                        {"replacement_normalizations": replacement_normalizations}
                        if replacement_normalizations
                        else {}
                    ),
                }
            )
            if call.name not in advertised or call.name not in STUDENT_V2_TOOL_NAMES:
                messages.append(
                    _tool_message(call.id, {"ok": False, "error": "Capability unavailable."})
                )
                continue

            if constraint_input_error == "AMBIGUOUS_PIN":
                constraint_input_refused = True
                local_result = {
                    "tool": call.name,
                    "ok": False,
                    "error_code": "AMBIGUOUS_PIN",
                    "error": (
                        "The section pin is ambiguous. Ask the student to name exactly "
                        "one course code and one section label for each pin. No timetable "
                        "was built without that constraint."
                    ),
                    "constraints_satisfied": False,
                }
                # The normal remote projector retains the fixed error envelope
                # while dropping implementation-only fields. Nothing identifying
                # or database-backed is carried by this refusal.
                provider_result = project_channel_tool_result(
                    call.name,
                    boundary.project_tool_result(call.name, local_result),
                    profile=channel_profile,
                )
                local_result = project_channel_tool_result(
                    call.name, local_result, profile=channel_profile
                )
                local_results.append(local_result)
                messages.append(_tool_message(call.id, provider_result))
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
                raw_local_result = execute_student_v2_tool(
                    call.name,
                    arguments,
                    principal=principal,
                    context=(
                        comparison_tool_context
                        if call.name == "course_choice_comparison"
                        else replacement_tool_context
                        if call.name == "feasible_course_replacements"
                        else tool_context
                    ),
                )
                if language == "Arabic":
                    raw_local_result = _prefer_arabic_course_names_in_payload(raw_local_result)
                provider_result = project_channel_tool_result(
                    call.name,
                    boundary.project_tool_result(call.name, raw_local_result),
                    profile=channel_profile,
                )
                local_result = project_channel_tool_result(
                    call.name, raw_local_result, profile=channel_profile
                )
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
        if verified_what_if and not requires_feasible_replacement:
            answer = _safe_graduation_answer(language, local_results, answer_style)
            if answer:
                graduation_safe_fallback_used = True
                break
        verified_comparison = any(
            row.get("tool") == "course_choice_comparison" and row.get("ok") for row in local_results
        )
        if verified_comparison and not requires_feasible_replacement:
            answer = _safe_course_comparison_answer(language, local_results, answer_style)
            if answer:
                comparison_safe_fallback_used = True
                break
        verified_replacement = any(
            row.get("tool") == "feasible_course_replacements" and row.get("ok")
            for row in local_results
        )
        if verified_replacement:
            answer = _safe_feasible_replacement_answer(language, local_results, answer_style)
            if answer:
                replacement_safe_fallback_used = True
                break

    if not answer:
        if requires_feasible_replacement:
            answer = (
                "تعذّر تشغيل فحص الاستبدال الموثوق؛ لذلك لن أقترح استبدالًا من دون "
                "دليل أكاديمي وجدول مكتمل بلا تعارضات. أعد المحاولة، علمًا بأن "
                "تسجيلك الفعلي والجدول المسجّل فعليًا لم يتغيّرا."
                if language == "Arabic"
                else (
                    "I could not run the verified replacement check, so I will not suggest "
                    "a swap without both academic and complete-timetable evidence. Try again; "
                    "your real registration and timetable were not changed."
                )
            )
            replacement_safe_fallback_used = True
        elif requires_course_comparison:
            answer = (
                "تعذّر تشغيل المقارنة الموثوقة بين المقررات المحددة؛ لذلك لن أفضّل "
                "مقررًا من دون دليل. أعد المحاولة مع ذكر رموز المقررات."
                if language == "Arabic"
                else (
                    "I could not run the verified comparison for those courses, so I will "
                    "not choose one without evidence. Try again with the exact course codes."
                )
            )
            comparison_safe_fallback_used = True
        else:
            if not any(row.get("tool") != "policy_lookup" for row in local_results):
                fallback_name = channel_fallback_tool(profile=channel_profile)
                fallback_raw = execute_student_v2_tool(
                    fallback_name,
                    {},
                    principal=principal,
                    context=tool_context,
                )
                if language == "Arabic":
                    fallback_raw = _prefer_arabic_course_names_in_payload(fallback_raw)
                try:
                    fallback_provider = project_channel_tool_result(
                        fallback_name,
                        boundary.project_tool_result(fallback_name, fallback_raw),
                        profile=channel_profile,
                    )
                except Exception:
                    fallback_provider = boundary.refusal_result(fallback_name)
                fallback_local = project_channel_tool_result(
                    fallback_name, fallback_raw, profile=channel_profile
                )
                local_results.append(fallback_local)
                tools_called.append(
                    {
                        "name": fallback_name,
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

    if constraint_input_refused:
        answer = (
            "طلب تثبيت الشعبة غير مكتمل. اذكر مقررًا واحدًا وشعبة واحدة لكل طلب، "
            "مثل: «ثبّت الشعبة M2 للمقرر AI331 وأنشئ الجدول المقترح مع الإبقاء "
            "عليها». لم يُنشأ جدول من دون القيد المطلوب، ولم يتغيّر تسجيلك الفعلي."
            if language == "Arabic"
            else (
                "The section pin is not specific enough. Name exactly one course and one "
                "section for each pin, for example: ‘Pin AI331 section M2 and build around "
                "it.’ I did not build an unconstrained timetable, and your actual "
                "registration was not changed."
            )
        )
        answer = _apply_saudi_register(answer, language, answer_style)

    if requires_feasible_replacement:
        safe_replacement = _safe_feasible_replacement_answer(language, local_results, answer_style)
        if safe_replacement:
            answer = safe_replacement
            replacement_safe_fallback_used = True
        else:
            answer = (
                "تعذّر تشغيل فحص الاستبدال الموثوق؛ لذلك لن أقترح استبدالًا من دون "
                "دليل أكاديمي وجدول مكتمل بلا تعارضات. لم يتغيّر تسجيلك الفعلي ولا "
                "الجدول المسجّل فعليًا."
                if language == "Arabic"
                else (
                    "I could not run the verified replacement check, so I will not suggest "
                    "a swap without both academic and complete-timetable evidence. Your "
                    "real registration and timetable were not changed."
                )
            )
            answer = _apply_saudi_register(answer, language, answer_style)
            replacement_safe_fallback_used = True

    if requires_course_comparison:
        safe_comparison = _safe_course_comparison_answer(language, local_results, answer_style)
        if safe_comparison:
            answer = safe_comparison
            comparison_safe_fallback_used = True
        else:
            answer = (
                "تعذّر تشغيل المقارنة الموثوقة بين المقررات المحددة؛ لذلك لن أفضّل "
                "مقررًا من دون دليل. أعد إرسال رموز مقررين إلى أربعة مقررات."
                if language == "Arabic"
                else (
                    "I could not run the verified comparison for those courses, so I will "
                    "not choose one without evidence. Send two to four exact course codes."
                )
            )
            comparison_safe_fallback_used = True

    safe_section = _safe_section_answer(language, local_results, answer_style)
    if (
        requires_section_check
        and safe_section
        and _section_answer_contradicts_evidence(answer, local_results)
    ):
        answer = safe_section
        section_safe_fallback_used = True

    safe_graduation = _safe_graduation_answer(language, local_results, answer_style)
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
    graduation_baseline_label_corrected = any(
        row.get("tool") == "graduation_progress"
        and row.get("ok")
        and _mislabels_planning_baseline_as_current(answer, row)
        for row in local_results
    )
    missing_required_what_if = requires_graduation_what_if and not graduation_what_if
    if missing_required_what_if:
        answer = (
            "تعذّر تشغيل مقارنة التغيير المطلوب على المقررات المرجعية المستخدمة في "
            "المحاكاة؛ لذلك لن أعرض التقدير المرجعي على أنه نتيجة للسيناريو المطلوب. "
            "لم يتغيّر أي مقرر أو الجدول المسجّل فعليًا."
            if language == "Arabic"
            else (
                "The requested planning-baseline course comparison could not be run, so I will "
                "not present the unchanged baseline as if it answered the scenario. No real "
                "course or timetable was changed."
            )
        )
        answer = _apply_saudi_register(answer, language, answer_style)
        graduation_safe_fallback_used = True
    elif safe_graduation and (
        graduation_what_if
        or incomplete_graduation
        or graduation_baseline_label_corrected
        or _graduation_revision_facts(answer, local_results)
        or _GRADUATION_UNSUPPORTED_INFERENCE.search(answer or "")
    ):
        answer = safe_graduation
        graduation_safe_fallback_used = True

    if _internal_output_markers(answer):
        answer = _humanise_internal_output_markers(answer, language)
        answer = _apply_saudi_register(answer, language, answer_style)
        internal_output_sanitized = True

    presentation = (
        replacement_timetable_presentation_from_tool_results(local_results)
        or graduation_presentation_from_tool_results(local_results)
        or timetable_presentation_from_tool_results(local_results)
    )
    if presentation:
        answer = remove_false_media_incapability(answer)

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
            answer = _citation_refusal(language, answer_style)

    portal_claim_refused = _claims_portal_action(answer)
    if portal_claim_refused:
        answer = _portal_boundary_response(language, answer_style)

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
        "presentation": presentation,
        "agent": {
            "version": "student-v2",
            "answer_style": answer_style,
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
            "course_comparison_grounding_required": requires_course_comparison,
            "course_comparison_reprompted": comparison_tool_reprompted,
            "course_comparison_safe_fallback_used": comparison_safe_fallback_used,
            "replacement_grounding_required": requires_feasible_replacement,
            "replacement_reprompted": replacement_tool_reprompted,
            "replacement_safe_fallback_used": replacement_safe_fallback_used,
            "graduation_grounding_required": requires_graduation_progress,
            "graduation_what_if_required": requires_graduation_what_if,
            "graduation_tool_reprompted": graduation_tool_reprompted,
            "graduation_what_if_reprompted": graduation_what_if_reprompted,
            "graduation_what_if_missing": missing_required_what_if,
            "graduation_reprompted": graduation_reprompted,
            "graduation_safe_fallback_used": graduation_safe_fallback_used,
            "graduation_baseline_label_corrected": graduation_baseline_label_corrected,
            "policy_uncertainty_reprompted": policy_uncertainty_reprompted,
            "internal_output_reprompted": internal_output_reprompted,
            "internal_output_sanitized": internal_output_sanitized,
            "constraint_input_refused": constraint_input_refused,
            "read_only": True,
            "portal_action": "student_manual_only",
        },
    }


def answer_student_advisor(**kwargs: Any) -> dict[str, Any]:
    """Feature-flagged seam used by the durable student conversation endpoint."""
    # The legacy runtime has no channel-specific evidence projection. Telegram
    # therefore stays on V2 even during a web rollback of the V2 feature flag;
    # silently downgrading here would reopen exact-record access.
    if is_enabled() or is_telegram_safe_profile(kwargs.get("channel_profile")):
        return answer_student_advisor_v2(**kwargs)
    from core.services.virtual_advisor import answer_virtual_advisor

    legacy_kwargs = dict(kwargs)
    legacy_kwargs.pop("channel_profile", None)
    return answer_virtual_advisor(**legacy_kwargs)


__all__ = [
    "FORBIDDEN_STUDENT_V2_TOOLS",
    "STUDENT_V2_TOOL_NAMES",
    "answer_student_advisor",
    "answer_student_advisor_v2",
    "execute_student_v2_tool",
    "is_enabled",
    "student_v2_tool_schemas",
]

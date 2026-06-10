# Virtual Advisor Capability Map

Snapshot date: 2026-05-14 (registry + agent loop landed 2026-06-11)

This map defines what the Virtual Advisor should reuse before anyone adds a new
function, API, model table, or screen. The advisor should behave like an
academic decision copilot, but its intelligence must come from verified local
services first and LLM wording second.

## Agent Loop + Capability Registry (implemented 2026-06-11)

The "Recommended Next Build Slice" below is now built:

- `core/services/virtual_advisor_capabilities.py` — read-only
  `AdvisorCapabilityRegistry`; each capability wraps an existing service
  with a JSON schema, allowed roles, and a scope-enforcing executor.
  Registered today: `find_students`, `get_student_context`,
  `lookup_course`, `recommend_courses`, `course_eligibility`,
  `graduation_shortfall`, `portfolio_triage`, `aggregate_demand`.
- `LocalLLMClient.chat_with_tools(...)` — native OpenAI-style function
  calling against LM Studio.
- `answer_virtual_advisor(...)` runs a tool-calling loop (default max 5
  iterations / 12 calls): the model decides which capabilities to call,
  observes results, and answers from gathered evidence. The legacy regex
  planner still runs first as seed evidence, and the single-shot path is
  the automatic fallback (old clients, flag off, or model without tool
  support). A student-id grounding check triggers one corrective retry
  when an answer cites ids absent from the evidence.
- Kill-switch: `VIRTUAL_ADVISOR_AGENT_LOOP_ENABLED=false` (env) reverts
  to the pre-loop behaviour with no redeploy. Budgets:
  `VIRTUAL_ADVISOR_MAX_TOOL_ITERATIONS`, `VIRTUAL_ADVISOR_MAX_TOOL_CALLS`.
- Identity/scope rules are enforced in executors server-side
  (`_resolve_scoped_student_id`, `_resolve_scoped_programs`) — never from
  model-supplied arguments.
- Tests: `tests/test_virtual_advisor_agent_loop.py` plus the legacy
  suite which now pins the fallback path.

### Live-tested hardening (cycles 1-3, 2026-06-11)

Four live batteries (28 realistic AR/EN questions against LM Studio
qwen3.6-35b-a3b across student/advisor/general-advisor scopes) drove
three fix cycles. Battery 1: 3/8 hard failures; battery 4: 0 failures,
all answers correct and grounded, latency 19-121s.

- 9th capability: `course_prerequisites` (all roles; per-program prereq
  codes incl. hour rules + plan term/credits).
- Loop turn failures (timeout / reasoning-budget) degrade to a forced
  final answer from gathered evidence instead of HTTP 503.
- Loop mode skips the regex seed (it dumped 100 unfiltered rows ≈13k
  prompt tokens and invited sample-based wrong answers); the model
  queries precisely instead.
- `find_students`: `name_contains` filter; agent payload capped at 30
  rows with `summary_stats` (gpa min/avg/max, below-2 count, avg
  credits) over all matched rows — overview questions answer from
  stats (230s → 35s, 22k → 5k tokens).
- Deterministic `answer_language` pin (Arabic-script detection) stops
  language drift; `programme_totals` in student context stops the
  model assuming a "standard" 132-hour degree (exact plan totals).
- Hijri sanity guard: model-supplied `academic_year` outside 1400-1500
  (it once sent 2024) or `term` outside 1-3 falls back to configured
  defaults; explicit legitimate terms pass through.
- Tunables: `VIRTUAL_ADVISOR_LOOP_MAX_TOKENS` (3000),
  `VIRTUAL_ADVISOR_TOOL_TURN_TIMEOUT_SECONDS` (75),
  `VIRTUAL_ADVISOR_MAX_TOOL_ITERATIONS` (5),
  `VIRTUAL_ADVISOR_MAX_TOOL_CALLS` (12).

## Non-Negotiables

- Do not add a new endpoint when an existing endpoint or service already answers the intent.
- Do not change response shapes used by existing screens unless the caller is updated and tested.
- Keep student answers scoped to one authenticated student identity.
- Keep advisor answers scoped by `require_student_scope`, `require_program_scope`, and `get_user_scope`.
- Never let the LLM invent grades, approvals, prerequisites, graduation status, rooms, or schedules.
- Every answer should be able to name the evidence source: student context, tool result, report, planner context, or timetable evidence.

## Current Entry Points

| Surface | Existing path or function | Current purpose | Reuse rule |
|---|---|---|---|
| Virtual advisor page | `GET /virtual-advisor/` | Existing chat workspace | Keep UI contract stable |
| Chat API | `POST /ops/virtual-advisor/chat/` | LLM answer grounded in verified context | Preferred chat entry |
| Tool preview API | `POST /ops/virtual-advisor/tools/preview/` | Shows deterministic dataset results before/with answer | Reuse for advisor dataset queries |
| Health API | `GET /ops/virtual-advisor/health/` | Local model health and model list | Reuse for runtime status |
| WhatsApp gateway | `whatsapp_gateway.services.process_inbound_text(...)` | Channel adapter into `answer_virtual_advisor(...)` | Keep as adapter, not second advisor brain |

## Capability Registry

| Intent family | Example user wording | Existing service or endpoint | Scope guard | Evidence returned today | Current status |
|---|---|---|---|---|---|
| Student profile snapshot | "How am I doing?", "Am I okay?", "Where do I stand?" | `build_verified_student_context(...)`; `GET /report/student-plan/` | `require_student_scope` or direct student WhatsApp scope | GPA, earned/current credits, passed/studying, remaining requirements, recommendations | Covered |
| Next-term recommendation | "What should I take next?", "register next term" | `recommend_next_courses(...)`; `GET /recommend/<student_id>/` | `require_student_scope` | Recommended course codes, count, academic year/semester | Covered |
| Prerequisite explanation | "Why cannot I take AI431?", "what blocks this course?" | `GET /report/student-plan/`; `GET /report/prerequisites/`; `build_course_eligibility_report(...)` | Student or program scope | Prerequisites, missing prereqs, can_register flags, blocked samples | Covered; needs better intent routing |
| Graduation / remaining plan | "How many courses left?", "am I graduating?" | `_build_student_plan_payload(...)`; `build_verified_student_context(...)`; `run_shortfall_analysis(...)` | Student scope | Not-taken courses, locked courses, blocker hints, zero-recommendation status | Mostly covered; shortfall has no direct advisor chat tool yet |
| Dataset finder | "List AI girls who passed AI331 and have 90+ hours" | `find_students_tool(...)`; `run_planned_tools(...)` | `get_user_scope` inside tool | Student rows, count, filters, applied scope, course status evidence | Covered for common filters |
| Advisor portfolio triage | "Who needs attention?", "show my risky students" | `list_students_by_advisor(...)`; `GET /report/students-by-advisor/` | Advisor/general/super-admin role rules | GPA risk, zero hours, high-priority missing, risk score, attention reasons | Covered; should be routed before custom SQL |
| Advisor roster export | "Export my advisees", "download this list" | `GET /export/students-by-advisor.csv` | Role and advisor scope | CSV with advisor/student/risk fields | Covered |
| Aggregate course demand | "What courses are most needed next term?" | `build_aggregate_counts(...)`; `GET /report/summary/`; aggregate exports | Program scope | Top recommended courses, counts, student count | Covered |
| Recommendation debugging | "Why did the system recommend these courses?" | `build_recommendation_debug_report(...)`; `GET /report/recommendation-debug/` | Program scope | Per-student passed/studying/recommended/prereq statuses | Covered |
| Missing high-priority courses | "Who is missing important unlock courses?" | `run_missing_high_priority_report(...)`; `GET /report/missing-high-priority/` | Program scope | Students, missing-this-parity, missing-other, scores | Covered |
| Course eligibility | "Who can take DS331?", "eligible for AI431" | `build_course_eligibility_report(...)`; `GET /report/course-eligibility/` | Program scope | Eligible IDs, blocked count, top missing prereqs | Covered |
| Program plan lookup | "Show AI plan", "what is in term 5?" | `GET /report/program-plan/` | Program scope | Course code, course name, term, credit hours | Covered |
| Planner context | "Build this student's schedule", "show current sections" | `POST /ops/planner/context/` | Staff and student scope | Student summary, baseline sections, recommendations | Covered for staff screen; chat should read, not mutate |
| Planner build | "Can this set of courses fit?" | `POST /ops/planner/build/` | Staff role | Candidate plan result from existing planner builder | Covered for planner screen; chat needs careful read-only preview policy |
| Section catalog | "What sections exist for these courses?" | `POST /ops/planner/sections-catalog/` | Staff role | Term sections and meetings | Covered |
| Section planning demand | "How many sections should we open?" | `ops/section-planning/*`; `core.services.section_planning` | General advisor/super admin | Generated section plan, course demand, capacity overrides | Covered |
| Timetable conflict risk | "Will these courses conflict?", "what overlaps?" | `build_conflict_matrix_report(...)`; timetable workspace conflict services | Program or timetable role scope | Course-pair conflict matrix, board conflicts, affected student evidence | Covered |
| Timetable workspace evidence | "Why is this placement bad?", "who is affected?" | `preview_placement_student_evidence(...)`; `detect_board_conflicts(...)`; `check_publish_readiness(...)` | Timetable workspace role guards | Affected students, conflicts, readiness blockers/warnings | Covered |
| Exam timetable | "Build exam timetable", "export exam plan" | `ops/exam-timetable/*`; `core.services.exam_timetable` | Super admin | Exam runs, draft impact, room assignment, exports | Covered; not student chat scope |
| Data import/admin | "Import plan", "check DB integrity" | `ops/db/*` | Super admin | Preview/import/integrity reports | Covered; never expose to student chat |
| Audit trail | "Who changed this?", "show advisor activity" | `query_audit_logs(...)`; audit endpoints | Super admin/general advisor guards | Audit rows, hash validation, CSV | Covered |

## Existing Scope Model

| Role | Data boundary | Existing source |
|---|---|---|
| `SUPER_ADMIN` | All students/programs | `get_user_scope`, `role_required`, policy helpers |
| `GENERAL_ACADEMIC_ADVISOR` | Configured departments | `UserScope.departments`, `require_program_scope`, `require_student_scope` |
| `ADVISOR` | Assigned `advisor_id` students | `UserScope.advisor_id`, `require_student_scope`, `list_students_by_advisor` |
| WhatsApp student | One linked `student_id` | `WhatsAppUserLink`, direct scope `{"role": "STUDENT", "student_id": ...}` |

Note: Django UI role groups currently center on staff roles. WhatsApp student scope is already enforced through direct service scope, not through a full student dashboard login role.

## How The Advisor Should Think

1. Identify whether the user is asking about one student, a cohort, a course, a program, a timetable, an export, or system administration.
2. Resolve identity and scope before retrieving data.
3. Use the existing service with the narrowest authority.
4. Put structured evidence into `verified_context`.
5. Let the LLM explain, summarize, ask a clarifying question, or format a result.
6. Log the action and keep the evidence available for the UI panel or channel transcript.

## Student Mode Expectations

Student mode must support natural, unclear wording such as:

- "I finished the AI thing, what now?"
- "Can I take project?"
- "Why I cannot register DS331?"
- "How many hours left?"
- "Am I in danger?"

It should reuse:

- `build_verified_student_context(...)` for profile, passed/studying, remaining requirements, and recommendations.
- `recommend_next_courses(...)` for next-term candidates.
- `_build_student_plan_payload(...)` for term-by-term plan status and blocked courses.
- `build_course_eligibility_report(...)` only within the student's own program and question context.

It must not expose:

- Other students.
- Advisor cohort lists.
- DB admin/import details.
- Timetable workspace internals unless they are reduced to the student's own schedule impact.

## Advisor Mode Expectations

Advisor mode must support broad or messy wording such as:

- "Who needs attention?"
- "Find girls in AI who already did AI331 and have 90+ hours."
- "Which DS students can take DS331?"
- "Export that list."
- "Why are my students blocked?"
- "What courses have the most demand?"

It should reuse:

- `find_students_tool(...)` for deterministic cohort filtering.
- `list_students_by_advisor(...)` for portfolio triage and attention reasons.
- `build_course_eligibility_report(...)` for course eligibility.
- `build_recommendation_debug_report(...)` for explanation-level evidence.
- `run_missing_high_priority_report(...)` for high-priority missing courses.
- `build_aggregate_counts(...)` and report/export endpoints for demand and downloads.

## Implementation Guardrails For Future Work

When adding new advisor behavior, follow this order:

1. Add or update tests showing the natural wording and the expected existing service.
2. Reuse a service directly if the chat backend can call it safely.
3. Reuse an endpoint only when the same HTTP contract is needed by UI callers.
4. Add a small adapter only if the existing service output needs LLM-friendly evidence shape.
5. Add a new service only if no existing service covers the verified data need.
6. Add a new endpoint only if a screen/channel genuinely needs a new public contract.

## Current Gaps Are Glue, Not Missing Product

| Gap | Why it matters | Best next step |
|---|---|---|
| ~~No explicit intent registry in code~~ | DONE 2026-06-11 | `virtual_advisor_capabilities.py` registry powers the agent loop |
| ~~Student vague course names~~ | DONE 2026-06-11 | `lookup_course` capability resolves name fragments to codes |
| Advisor follow-up memory | "only females", "export that", "what about DS?" needs session state | Keep session-scoped history and last tool result metadata, not global memory |
| Export follow-up from chat | Results can be shown, but "export that" is not a first-class intent | Reuse existing export endpoints where possible; otherwise add adapter that writes the last tool result |
| ~~Shortfall analysis not wired to chat~~ | DONE 2026-06-11 | `graduation_shortfall` capability wraps `run_shortfall_analysis(...)` |
| Timetable evidence is powerful but broad | Chat should not mutate timetable state casually | Start with read-only evidence tools: conflicts, affected students, readiness |
| Full student web login role | WhatsApp student scope exists; Django staff RBAC is staff-first | Add only if student portal UI is needed, keeping WhatsApp scope separate |

## Regression Tests To Protect Existing Screens

Before merging advisor changes, run at minimum:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_virtual_advisor.py tests\test_advisor_endpoints.py tests\test_report_exports.py tests\test_recommend_endpoint.py tests\test_role_matrix_e2e.py
.\.venv\Scripts\python.exe manage.py check
```

For timetable-facing advisor features, also include the relevant timetable workspace, section planning, and conflict tests.

## Recommended Next Build Slice

The safest next implementation is not a new screen. It is an internal
`VirtualAdvisorCapability` registry inside the existing virtual advisor service
that describes:

- intent name
- sample phrasings
- service function to reuse
- required role or scope
- evidence fields to include
- whether the action is read-only, export-only, or mutating

That registry should initially be read-only and should route only to existing
services. It can then power both `/ops/virtual-advisor/chat/` and WhatsApp
without duplicating business logic.

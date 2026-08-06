# Routing audit — the 22 router/spec mismatches

Commit 6A.1. The v1.0 evaluation contract (`evals/advisor/planner_priority_eval_v1.yaml`)
and `advisor_intent.classify_intent` disagree on 22 of 50 cases. **The target is not
50/50 family-string equality.** Several rows over-specify a single intent where the
correct behaviour is contextual, clarification-driven, or valid under more than one
closely related family, so each mismatch is classified before any router change is made.

Only the `ROUTER_BUG` rows are in scope for commit 6A.2.

## Totals

| Classification | Count | Meaning |
|---|---|---|
| `ROUTER_BUG` | 5 | Wrong behaviour or wrong tool surface — **fix in 6A.2** |
| `SPEC_BUG` | 7 | The YAML requires the wrong family — fix the spec, not the router |
| `ALIAS_EQUIVALENT` | 6 | Different family, same safe behaviour |
| `CLARIFICATION_REQUIRED` | 2 | An essential entity is absent; must ask, not execute |
| `CONTEXT_REQUIRED` | 1 | Needs a prior turn, a draft, or a referenced course |
| `NO_ROUTE_REQUIRED` | 1 | Seeded evidence or a deterministic explanation suffices |

**5 of 22** are actual router defects.

## The table

| Case | Spec intent | Mode | Expected | Allowed | Actual | Actual tool surface | Domain | Classification |
|---|---|---|---|---|---|---|---|---|
| TT09 | `PLANNER_EDIT_DRAFT` | `one_of` | `PLANNER_BUILD` | PLANNER_BUILD, PLANNER_EDIT_DRAFT | `PLANNER_BUILD` | `build_my_timetable` | `PLANNER_DATA` | **`SPEC_BUG`** |
| TT10 | `PLANNER_EDIT_DRAFT` | `exact` | `PLANNER_EDIT_DRAFT` | PLANNER_EDIT_DRAFT | `GENERAL_AGENT` | `full surface` | `PLANNER_DATA` | **`ROUTER_BUG`** |
| TT12 | `CURRENT_TIMETABLE` | `one_of` | `CURRENT_TIMETABLE` | CURRENT_TIMETABLE, GENERAL_AGENT | `GENERAL_AGENT` | `full surface` | `TIMETABLE_DATA` | **`ALIAS_EQUIVALENT`** |
| TT13 | `CURRENT_TIMETABLE` | `one_of` | `CURRENT_TIMETABLE` | CURRENT_TIMETABLE, GENERAL_AGENT | `GENERAL_AGENT` | `full surface` | `TIMETABLE_DATA` | **`ALIAS_EQUIVALENT`** |
| TT17 | `TIMETABLE_CLASH` | `exact` | `GENERAL_AGENT` | GENERAL_AGENT | `GENERAL_AGENT` | `full surface` | `TIMETABLE_DATA` | **`SPEC_BUG`** |
| TT19 | `PLANNER_BUILD` | `clarify` | `null` | GENERAL_AGENT | `GENERAL_AGENT` | `full surface` | `PLANNER_DATA` | **`CLARIFICATION_REQUIRED`** |
| TT20 | `MIXED` | `one_of` | `GENERAL_AGENT` | GENERAL_AGENT, MIXED | `GENERAL_AGENT` | `full surface` | `COURSE_DATA` | **`SPEC_BUG`** |
| TT21 | `PLANNER_BUILD` | `contextual` | `GENERAL_AGENT` | GENERAL_AGENT | `GENERAL_AGENT` | `full surface` | `PLANNER_DATA` | **`CONTEXT_REQUIRED`** |
| TT22 | `PLANNER_BUILD` | `exact` | `GENERAL_AGENT` | GENERAL_AGENT | `GENERAL_AGENT` | `full surface` | `PLANNER_DATA` | **`SPEC_BUG`** |
| TT23 | `PLANNER_SELECT_PREFERRED` | `exact` | `GENERAL_AGENT` | GENERAL_AGENT | `GENERAL_AGENT` | `full surface` | `PLANNER_DATA` | **`SPEC_BUG`** |
| TT25 | `PLANNER_SELECT_PREFERRED` | `none` | `null` | GENERAL_AGENT | `GENERAL_AGENT` | `full surface` | `PLANNER_DATA` | **`NO_ROUTE_REQUIRED`** |
| TT30 | `PLANNER_VIEW_ALTERNATIVES` | `exact` | `GENERAL_AGENT` | GENERAL_AGENT | `GENERAL_AGENT` | `full surface` | `PLANNER_DATA` | **`SPEC_BUG`** |
| CP02 | `COURSE_PRIORITY` | `exact` | `COURSE_PRIORITY` | COURSE_PRIORITY | `COURSE_UNLOCKS` | `why_course_locked` | `COURSE_DATA` | **`ROUTER_BUG`** |
| CP07 | `COURSE_PRIORITY` | `one_of` | `COURSE_PRIORITY` | COURSE_PRIORITY, COURSE_UNLOCKS, GENERAL_AGENT | `GENERAL_AGENT` | `full surface` | `COURSE_DATA` | **`ALIAS_EQUIVALENT`** |
| CP08 | `COURSE_UNLOCKS` | `one_of` | `COURSE_UNLOCKS` | COURSE_UNLOCKS, COURSE_PRIORITY, GENERAL_AGENT | `GENERAL_AGENT` | `full surface` | `COURSE_DATA` | **`ALIAS_EQUIVALENT`** |
| CP09 | `COURSE_PRIORITY` | `exact` | `GENERAL_AGENT` | GENERAL_AGENT | `GENERAL_AGENT` | `full surface` | `COURSE_DATA` | **`SPEC_BUG`** |
| CP12 | `COURSE_LOCK_REASON` | `clarify` | `null` | — | `COURSE_PRIORITY` | `my_progress` | `COURSE_DATA` | **`CLARIFICATION_REQUIRED`** |
| CP14 | `COURSE_PRIORITY` | `exact` | `COURSE_PRIORITY` | COURSE_PRIORITY | `GENERAL_AGENT` | `full surface` | `COURSE_DATA` | **`ROUTER_BUG`** |
| CP16 | `COURSE_PRIORITY` | `one_of` | `COURSE_PRIORITY` | COURSE_PRIORITY, COURSE_UNLOCKS | `GENERAL_AGENT` | `full surface` | `COURSE_DATA` | **`ROUTER_BUG`** |
| CP18 | `COURSE_PRIORITY` | `one_of` | `COURSE_PRIORITY` | COURSE_PRIORITY, GENERAL_AGENT | `GENERAL_AGENT` | `full surface` | `COURSE_DATA` | **`ALIAS_EQUIVALENT`** |
| CP19 | `COURSE_PRIORITY` | `one_of` | `COURSE_PRIORITY` | COURSE_PRIORITY, GENERAL_AGENT | `GENERAL_AGENT` | `full surface` | `COURSE_DATA` | **`ALIAS_EQUIVALENT`** |
| CP20 | `COURSE_PRIORITY` | `exact` | `COURSE_PRIORITY` | COURSE_PRIORITY | `COURSE_UNLOCKS` | `why_course_locked` | `COURSE_DATA` | **`ROUTER_BUG`** |

## Reasons

### TT09 — `SPEC_BUG`

«ثبّت لي شعبة M2 في مقرر AI331، وابنِ بقية الجدول حولها.» carries the build imperative «وابنِ» beside «الجدول» and establishes NO existing draft — no past-tense edit verb, no reference to sections already chosen. Contrast TT10 («اخترتها يدويًا») and TT26 («عدّلت قائمة المقررات»), which do. Per the owner's ruling this is specification ambiguity, and the two YAMLs prove it: planner_priority_eval_v1.yaml:238-253 requires PLANNER_EDIT_DRAFT, FORBIDS build_my_timetable and demands expected_action OPEN_STUDENT_PLANNER/EDIT_DRAFT with requested_edit.section_label M2; planner_priority_batch.yaml:139-150 requires PLANNER_BUILD, REQUIRES build_my_timetable and expected_action null. Identical Arabic, contradictory contracts — unscoreable. Neither family is complete: build_my_timetable's schema (virtual_advisor_capabilities.py:2508-2536) accepts only student_id/must_include/max_credits/keep_current_sections/academic_year/term — there is no section-pin parameter, so PLANNER_BUILD honours «AI331» and silently DROPS «شعبة M2»; the EDIT_DRAFT route carries section_label to the planner but cannot build. Router is defensible, not defective.

### TT10 — `ROUTER_BUG`

«لا تغيّر الشعب التي اخترتها يدويًا، لكن غيّر باقي المقررات.» — «اخترتها» is PAST TENSE: the sections were already chosen, on a draft. _families() returns [] (measured), so the family is GENERAL_AGENT and the full role surface is advertised, INCLUDING build_my_timetable — the one tool eval_v1:268-273 explicitly forbids for this case. This is not a benign superset: advisor_actions.py:69-75 states that _exec_build_my_timetable 'has no access to a draft and no way to learn which courses the student added or dropped', so answering with a build 'produces alternatives from the SYSTEM's list and presents them as based on your edit, which is a fabrication with a tool call behind it'. Wrong tool surface for the question. The correct answer is a deterministic route that already exists — _EDIT_DRAFT_HANDOFF, ROUTED_INTENTS[PLANNER_EDIT_DRAFT] (advisor_actions.py:315) — and it never fires. The gap is enumerable: _EDIT_WORD (advisor_intent.py:289-300) lists عدلت/غيرت/حذفت/اضفت but not «اخترت», and the module comment at 286-288 correctly excludes the IMPERATIVE «غيّر» while leaving the past-tense selection verb uncovered. Second half: batch.yaml:151-162 requires build_my_timetable for TT10, directly contradicting eval_v1's forbidden list — that half is a SPEC_BUG.

### TT12 — `ALIAS_EQUIVALENT`

«كم مقرر وكم ساعة مسجلة عندي حاليًا؟» names no «جدولي», so the CURRENT_TIMETABLE markers (_MY_SCHEDULE / _POSSESSIVE+_SCHEDULE, advisor_intent.py:459-465) cannot fire. The abstention is safe under the owner's alias rule because BOTH families expose a tool that actually answers: my_timetable's payload returns registered_course_count and registered_credit_hours literally (virtual_advisor_capabilities.py:1257-1258), and get_student_context.course_evidence.current_term_registrations is the authoritative source named in the system prompt (virtual_advisor.py:125). GENERAL_AGENT advertises the full role surface, which contains both; eval_v1:318-325 allows exactly those two and requires NONE ('a new call is optional when context already contains it'). Measured: policy_intent(TT12) == () and advisor_intent is imported only by advisor_actions.py at this HEAD, so the abstention carries no policy obligation and no refusal risk. SEPARATE, REAL SPEC DEFECT (not the router's): batch.yaml:180-186 REQUIRES my_progress for TT12. my_progress is the degree-plan/unlock-ranking tool (virtual_advisor_capabilities.py:2251-2263) and holds no current-term registration count or credit total; requiring it contradicts eval_v1's own must_not_claim 'Do not substitute plan-status studying rows' and virtual_advisor.py:125 'never present it as the registration list'.

### TT13 — `ALIAS_EQUIVALENT`

«هل عندي مقرر مسجل لكن ما له وقت محاضرة في النظام؟» — again no «جدولي», so no CURRENT_TIMETABLE marker fires (_families() == [], measured). Both YAMLs agree on the TOOL and disagree only on the LABEL: eval_v1:342-348 requires my_timetable, batch.yaml:188-199 requires my_timetable. GENERAL_AGENT's full surface contains my_timetable, and that payload answers the question by name — courses_without_a_time, documented as 'genuinely registered; they simply have no meeting recorded' (virtual_advisor_capabilities.py:1259, 1263-1264), which is exactly the distinction eval_v1's must_assert demands ('Distinguish registration from missing meeting fields'). eval_v1 declares NO forbidden tool for TT13, so the wider surface costs nothing. Measured policy_intent == (): no obligation is picked up by abstaining. CURRENT_TIMETABLE is the tighter route and would be an improvement, but it is not a correctness difference.

### TT17 — `SPEC_BUG`

«النظام كتب أن CS372 غير موجود في البيانات؛ هل هذا يعني أن الجامعة ما طرحته؟» is a data-semantics question about NOT_ON_FILE. It contains no clash word at all — _CLASH (advisor_intent.py:214-228) lists تعارض/تتعارض/… and none appears — so TIMETABLE_CLASH could only fire by loosening a marker onto a sentence that is not about collisions. The spec label is self-refuting: TIMETABLE_CLASH's owning capability is my_clash_free_sections (CAPABILITY_FOR_FAMILY), and my_clash_free_sections appears in NEITHER eval_v1's required_any NOR its allowed list for TT17 (eval_v1:434-443 names lookup_course, my_timetable, get_student_context). The YAML names a family whose own tool its own tool_contract excludes. batch.yaml:240-252 gets it right (GENERAL_AGENT, lookup_course), and the full agent surface contains all three permitted tools. Router correct; eval_v1's intent field is the defect.

### TT19 — `CLARIFICATION_REQUIRED`

«المقرر موجود وله شعب، لكن ما دخل في أفضل جدول. ليش؟» names no course code — _COURSE_CODE finds nothing — and eval_v1:495-496 declares setup 'No previous planner result or course antecedent exists' with clarification_allowed: true and must_assert 'Ask which course the student means'. Under the owner's binding rule this is CLARIFICATION_REQUIRED, mode clarify, expected_family null, and the GENERAL_AGENT abstention is the CORRECT outcome, not a missing route pattern. BOTH halves of the spec are wrong, as the rule requires me to say: (1) eval_v1:480 labels the family PLANNER_BUILD, whose owning capability is build_my_timetable — a data tool this question must not execute, and eval_v1's own must_not_claim says 'Do not rebuild before identifying the course'; (2) batch.yaml:265-276 goes further and REQUIRES build_my_timetable with clarification_allowed: false, which mandates the exact rebuild eval_v1 forbids. A spec that names a concrete family for an entity-less question penalises the router for behaving correctly.

### TT20 — `SPEC_BUG`

«ليش ما ضفت AI491؟ هل المشكلة في المتطلب السابق أو في وقت الشعبة؟» needs TWO DATA capabilities — prerequisite state (why_course_locked) and planner/section evidence (build_my_timetable / my_clash_free_sections) — which is exactly what eval_v1:504-512 requires, with policy_contract.mode data_only. No marker fires: «المتطلب» is not in _COURSE_NOUN, «وقت الشعبة» contains no _CLASH word, and there is no build or unlock verb, so _families() == [] (measured). On TOOL SURFACE the mismatch is nil — MIXED has no entry in CAPABILITY_FOR_FAMILY and no ROUTED_INTENTS row, so labelling TT20 MIXED yields the same full surface GENERAL_AGENT already gives, and that surface contains both required tools. The defect is the LABEL'S MEANING. MIXED is defined in this router as cross-DOMAIN over _DOMAIN {planner, course, policy} (advisor_intent.py:528-544) and its documented purpose is 'a build request carrying a permission question'. The eval uses one word for two different things: TT08 is MIXED with policy_contract.mode required and required_policy_ids TU.LOAD.SEMESTER_RANGE; TT20 is MIXED with mode data_only. Measured corroboration that this is actively confusing: policy_intent(TT20) returns ('ELIGIBILITY','TOPIC:terminology') — the only non-empty result among all twelve cases — while the eval says data_only, so the string-level gate and the curated contract already disagree about whether TT20 owes a citation. See notes for the proposed primary_family/secondary_families/composition replacement.

### TT21 — `CONTEXT_REQUIRED`

«الجدول أضاف مقررًا أنا ما طلبته، من وين جاء؟» asks about a build that ALREADY happened. eval_v1:543-546 declares setup 'Previous planner result includes per-course source provenance' with evidence_required [student_requested_courses, system_recommended_courses] and required_all EMPTY — the answer is read from the prior turn's structured payload, not produced by a new call. The repo says the same in the executor's own comment (virtual_advisor_capabilities.py:1744-1747): merging the two lists 'is what left TT21 «الجدول أضاف مقررًا أنا ما طلبته، من وين جاء؟» unanswerable: the payload asserted the student had asked for all four courses' — the fix was to the PAYLOAD's provenance fields, never to the route. So no family owns this; the agent loop with the prior turn in context does. batch.yaml:290-302 REQUIRES recommend_courses + build_my_timetable, which would run a FRESH build whose provenance may differ from the result the student is actually pointing at — answering a question about run N with the output of run N+1. eval_v1:527 labelling it PLANNER_BUILD is the same error one step smaller. Router abstention is correct.

### TT22 — `SPEC_BUG`

I AGREE WITH THE REPO: SPEC_BUG, not ROUTER_BUG. «هل الشعب الموجودة في الجدول المقترح فيها مقاعد متاحة فعلًا؟» asks for seat availability, and NO capability holds it. build_my_timetable's own returned note says so verbatim (virtual_advisor_capabilities.py:1846-1848): 'not an offer of a seat - there are no seat counts in the data, so never say a section has room'; my_clash_free_sections repeats it at 2446 ('There are no seat counts, so never claim a section has room'); my_timetable at 1261 ('not a live seat count'). The complete planner payload key set, TIMETABLE_FACT_KEYS (timetable_provenance.py:104-117), is {student_id, using_timetable_of_term, student_requested_courses, system_recommended_courses, retained_sections, new_sections, fixed_sections, section_replacements, unplaced_courses, credit_summary} — no capacity field, no enrolment field. (max_capacity/current_enrollment exist only in the timetable-PLANNING models, e.g. timetable_assignment_models.py:52, and no student-facing capability exposes them.) So routing TT22 to PLANNER_BUILD hands the question the one tool that structurally cannot answer it — the failure mode the router's own docstring names at advisor_intent.py:24-27: 'routed to the planner returns a timetable and no seat count, and the answer that comes back is a fabrication with a tool call behind it'. eval_v1:551 labels the family PLANNER_BUILD while its own tool_contract sets required_all [] AND allowed [] — it names a family and then forbids every tool that family owns. batch.yaml:303-314 agrees with the router (GENERAL_AGENT, no tools). The fall-through is the designed correct behaviour.

### TT23 — `SPEC_BUG`

I AGREE WITH THE REPO: SPEC_BUG, not ROUTER_BUG. «اعتمد الخيار الأول وسجلني في الشعب الموجودة فيه.» is two demands, and «سجلني» — register me — is the one that matters. No family can serve it: the capability registry REFUSES to register a non-read-only tool (virtual_advisor_capabilities.py:222, 'Capability {name} is not read-only'), and advisor_actions.py:40-47 records that planner_drafts.select_alternative is deliberately kept out of the tool list because 'advertising that as a model tool would put a mutation behind an argument the model fills in'. The router's marker comment states the ruling at advisor_intent.py:271-274: «اعتمد الخيار الأول» is excluded from _OPTION because 'routing it to a save-preference surface would answer the half of the question that is harmless'. MEASURED HARM of the spec's family: PLANNER_SELECT_PREFERRED is in ROUTED_INTENTS (advisor_actions.py:314) with accepts_alternative_ref=True, and handoff_for_question short-circuits generation entirely — 'deterministic text… and no provider call at all' (advisor_actions.py:36). I ran alternative_ref_in on the exact question: it returns 'ALT_1'. So the spec's family would emit a hand-off asserting «تحديد جدول مفضّل … متاح» plus an ALT_1 ordinal, against a draft eval_v1:588 explicitly says does not exist ('No authoritative persisted option payload is supplied to answer generation') — affirming an action the student did not request and pointing at option contents nothing supplied. batch.yaml:315-326 agrees with the router. Abstention lets the loop produce the refusal the two must_asserts require.

### TT25 — `NO_ROUTE_REQUIRED`

«لما أحفظ جدولًا كمفضل، هل يتغير تسجيلي الحالي في البوابة؟» is a QUESTION ABOUT a command, not the command. BOTH YAMLs set required_tools/required_all to empty (eval_v1:620-625, batch.yaml:339-350): the complete answer is a deterministic explanation of product semantics — a planner preference is not a registration — which needs no evidence lookup at all. That is NO_ROUTE_REQUIRED. The router's exclusion is deliberate and documented at advisor_intent.py:276-281: «كمفضل» is omitted from _PREFERRED precisely so that this sentence, which contains the save verb AND the preference word, does not fire — 'Routing it to the planner would answer a question the student did not ask and leave the one they did ask unanswered.' I confirm the mechanism: _families() == [] (measured), and handoff_for_question returns None. Note the partial defence of the spec: _SELECT_PREFERRED_AR (advisor_actions.py:239-241) does contain «تفضيل جدول لن يسجّلك في أي شعبة، ولن يحذف أو يغيّر تسجيلك الرسمي», which satisfies both must_asserts — but it wraps them in «متاح … افتح المخطط الدراسي لتحديد الجدول الذي تفضّله», a call to action for an operation the student never requested. eval_v1:618 labelling the family PLANNER_SELECT_PREFERRED is therefore an over-route, not a miss.

### TT30 — `SPEC_BUG`

«أبغى في كل بديل أسماء المدرسين والقاعات ونوع المعمل وعدد المقاعد المتبقية.» asks for four fields the authoritative payload does not carry, and eval_v1:738 states the required behaviour as 'Explain which requested fields are absent'. Field-by-field: TIMETABLE_FACT_KEYS (timetable_provenance.py:104-117) has no instructor, room, lab-type or seat key; baseline_sections (timetable_provenance.py:172-177) says 'ONLY the fields named below survive. The baseline rows carry instructor and room, and _project_my_timetable drops instructor names on purpose' — each section row keeps only course_code, course_name, section, term_section_id, meetings; seat counts do not exist anywhere (virtual_advisor_capabilities.py:1847). PLANNER_VIEW_ALTERNATIVES is the WRONG family and provably so: it is in ROUTED_INTENTS (advisor_actions.py:313), so it SHORT-CIRCUITS generation with fixed text — _VIEW_ALTERNATIVES_AR/_EN say only 'I can offer you more than one proposed timetable… Open the study planner to see the alternatives' (advisor_actions.py:225-233) and mention none of the four missing fields. Routing TT30 there would make the eval's own must_assert unreachable by construction, because no provider call happens. batch.yaml:404-417 is wrong in the other direction: it requires build_my_timetable, whose payload lacks all four fields too. Router abstention is the only path that can produce the required explanation. (Mechanically: «كل» is not in _MORE_THAN_ONE and «جدول» is absent, so neither the alternatives nor the build markers fire.)

### CP02 — `ROUTER_BUG`

«أي مقرر عندي يفتح أكبر عدد من المقررات مباشرة؟» is a cross-course ranking over the student's own set and names no course code. The behavioural test the owner set is decisive against the routed family: why_course_locked declares "required": ["course_code"] (virtual_advisor_capabilities.py:2320) and its executor is documented «Explain ONE course: passed, studying, open now, or blocked and exactly why» (line 1010) — with no code in the sentence it cannot be invoked at all, let alone rank across a plan. my_progress is described as «the ONLY tool that ranks courses by unlock impact», returning unlock_impact_ranking ordered with sole_remaining_prerequisite_count per course plus most_useful_course_to_pass (lines 2253-2264) — literally «أكبر عدد ... مباشرة». So the two families do not both expose a tool that can answer, and this is a wrong tool surface, not an alias. Corroborating: planner_priority_batch.yaml contradicts itself here — expected_family COURSE_UNLOCKS (capability why_course_locked) but required_tools [my_progress], with why_course_locked only allowed.

### CP07 — `ALIAS_EQUIVALENT`

«رتّب لي AI331 وCS372 وAI352 حسب تأثير كل واحد على بقية الخطة.» names all three courses, so nothing is missing. The eval's contract is a single OR-group required_any [[my_progress, why_course_locked]], and GENERAL_AGENT advertises the whole role-permitted surface — a strict superset of both alternatives — so neither required tool is withheld. Measured, the abstention changes no behaviour: requires_policy_contract is False under GENERAL_AGENT (policy_intent returns ()), and False under COURSE_PRIORITY as well, so there is no citation obligation either way. my_progress names this use verbatim: «to rank or compare several courses by impact» (capabilities.py:2263-2264). Same safe behaviour, superset surface.

### CP08 — `ALIAS_EQUIVALENT`

«إذا أجلت AI331 فصلًا، وش المقررات التي قد تتأخر بسببه؟» names AI331, so the forward fields are reachable. The eval's required_all is why_course_locked AND my_progress — two capabilities owned by two different families under CAPABILITY_FOR_FAMILY, so no single narrow family satisfies the contract and GENERAL_AGENT's full surface is the only route exposing both. Measured: requires_policy_contract False (policy_intent ()), grounding retrieved but not owed, so the abstention has no downstream cost. why_course_locked supplies the direct half (sole_remaining_prerequisite_for) and the chain half (on_prerequisite_chain_of_count, capabilities.py:2300-2302), which is exactly the case's «Separate direct from downstream affected courses».

### CP09 — `SPEC_BUG`

Here the router is right and the eval YAML is wrong. «أنا أدرس AI331 حاليًا؛ هل يبقى ضمن المقررات التي توصي أن أسجلها؟» asks for membership of the recommendation set plus current enrolment status — precisely the eval's own required_all [recommend_courses, get_student_context]. But the YAML labels it intent COURSE_PRIORITY, whose owning capability is my_progress, and my_progress's own description rules itself out: «Broader than recommend_courses, which returns only the credit-capped suggestion for the coming term» (capabilities.py:2264-2265); recommend_courses is the tool that «Compute[s] the official next-term course recommendations ... using the verified recommender» (line 2096-2097). A family whose capability structurally cannot establish membership cannot be the expected family, so the YAML names the wrong one while the router's full surface keeps both required tools callable. Measured: requires_policy_contract False, grounding retrieved — no cost to the abstention.

### CP12 — `CLARIFICATION_REQUIRED`

Both halves are wrong. «هذا المقرر مقفل بأكثر من شرط؛ هل يعتبر عالي الأولوية رغم أن اجتياز مقرر واحد ما يفتحه؟» identifies no course; the eval's own setup says «No course antecedent exists», clarification_allowed is true, and required_all, required_any and allowed are ALL empty — the case's contract is call nothing and ask. (1) The router's COURSE_PRIORITY is wrong because its capability my_progress has no required parameters, so it executes a whole-plan priority analysis for an unidentified «هذا المقرر» and answers a question nobody asked. The route is deliberate, not incidental: advisor_intent.py:552-553 cites this exact sentence as the reason PRIORITY outranks UNLOCKS — «contains an unlock verb inside a subordinate clause about ranking». (2) The YAML's COURSE_LOCK_REASON is equally wrong: its capability why_course_locked declares "required": ["course_code"] (capabilities.py:2320), so with no antecedent it either cannot be called or the model must invent a code — the exact failure the case exists to catch («Do not invent a course, chain, or priority result»). Naming any concrete family for this question is a defect.

### CP14 — `ROUTER_BUG`

This abstention is not free, and the cost is measured rather than argued. «النظام يقول إن أحد المتطلبات غير معروف في الخطة؛ كيف يؤثر هذا على ترتيب الأولوية؟» is a data-quality question about a UNKNOWN_PREREQ flag, and the eval declares policy_contract.mode data_only. Measured at 5923f5f: requires_policy_contract(CP14, family=GENERAL_AGENT) is True, with policy_intent ('ELIGIBILITY', 'ENTITLEMENT'); routed to COURSE_PRIORITY the same question measures False, because GENERAL_AGENT is deliberately excluded from DATA_INTENTS (policy_contract.py:395) as the router's «I am not certain». So the abstention alone converts a data question into one owing a regulatory citation — the class commit 5923f5f exists to remove («a data question is no longer refused as a regulation»). CP11 is the precedent and I re-measured it: grounding none_governing, i.e. an outright refusal; CP14 measures grounding retrieved with 3 direct policies, so it is not refused but is compelled to cite a regulation for a plan-data flag. The evidence the case requires is inside COURSE_PRIORITY's own capability: UNKNOWN_PREREQ is emitted by build_unlock_report (student_unlock.py:180), which is the backend of my_progress (capabilities.py:947).

### CP16 — `ROUTER_BUG`

Same measured defect as CP14. «مقرر متطلباته مكتملة لكنه ما له شعبة في بيانات النظام؛ هل يظل مهمًا أكاديميًا؟» is two data reads — prerequisite completeness and section presence — and the eval encodes that as two OR-groups with policy_contract.mode data_only; what it demands («Say academic importance may remain even when no section is recorded», «Keep NOT_ON_FILE distinct from not offered») is data semantics, not a rule. Measured: requires_policy_contract is True under GENERAL_AGENT with policy_intent ('ELIGIBILITY',), and False under COURSE_PRIORITY, which is in DATA_INTENTS. Grounding measures retrieved (3 direct), so the turn is not refused but is held to a citation the case says it does not owe. MIXED is not a remedy either — it is likewise absent from DATA_INTENTS by design (policy_contract.py:386-389) — so COURSE_PRIORITY, or COURSE_UNLOCKS per the eval's first OR-group, is the only route that leaves the obligation off.

### CP18 — `ALIAS_EQUIVALENT`

«عندي مساحة لثلاثة مقررات فقط؛ اخترها بناءً على أثرها في فتح الخطة وبحد الساعات.» needs my_progress AND recommend_courses (the eval's required_all); GENERAL_AGENT's full surface exposes both, a superset of what COURSE_PRIORITY declares. Measured, there is no behavioural delta: requires_policy_contract is False under GENERAL_AGENT (policy_intent ()) and False under COURSE_PRIORITY, so unlike CP14/CP16 the abstention flips nothing. Recorded rather than scored: the eval sets policy_contract.mode conditional because «بحد الساعات» can require the load rule, yet the gate returns False on BOTH routes — the conditional half is unenforced whichever family wins, which is a gate gap and not a routing error.

### CP19 — `ALIAS_EQUIVALENT`

I AGREE with the repo, and the repo names this question by hand. advisor_intent.py:30-32 reads: «هل الشعب فيها مقاعد؟» (TT22) and «بصفتي المرشد، ما المقرر الذي يحرر أكبر عدد من المقررات؟» (CP19) «are both better served by the agent loop than by a family that would answer the wrong half». The measurement supports the argument. CP19's first must_assert is «Authorise scope before execution», and no family carries scope semantics — _DOMAIN maps COURSE_PRIORITY to «course» only, and DATA_INTENTS narrows nothing but the policy contract — so a confident COURSE_PRIORITY route would answer the ranking half of a cross-student question and skip the authorisation half silently. The abstention costs nothing measurable here: requires_policy_contract is False (policy_intent ()), and GENERAL_AGENT advertises the full role surface, a superset of the eval's required my_progress; the owner's own batch reaches the same place (expected_family GENERAL_AGENT, required_tools [my_progress]). requires_prior_context is true because the student is present only as the placeholder «[رقم الطالب]» in the eval text and is dropped entirely in the batch text, so the referent comes from the adviser's portfolio scope and the opaque student_ref named in setup, not from the sentence.

### CP20 — `ROUTER_BUG`

RECOMMENDED CONTRACT: option (a) — add setup.prior_course_code: AI331 and keep mode exact with COURSE_PRIORITY. Four reasons to prefer it over «clarify»: (i) both YAMLs already set clarification_allowed false for CP20 while setting it true for CP12 and CP13, so the owner's own files place CP20 outside the clarification class; (ii) expected_behavior is «Give an auditable priority explanation and state recommendation limits» and all three must_assert items are substantive claims a clarifying question cannot deliver; (iii) the eval REQUIRES why_course_locked, whose schema declares "required": ["course_code"] (capabilities.py:2320) — the contract is unsatisfiable without an antecedent, so supplying one is the minimal repair, whereas «clarify» would delete both required tools and the whole must_assert list, producing a different case rather than a fixed one; (iv) AI331 is the corpus's canonical course (CP03, CP04, CP05, CP06, CP08, CP09, and TT09 pins AI331-M2). WHY THE ROUTE IS STILL A BUG under that contract: «أعطني سبب ترتيبه» asks first for the RANKING BASIS, which only my_progress produces (unlock_impact_ranking, most_useful_course_to_pass, capabilities.py:2256-2259); why_course_locked analyses one named course and holds no ranking at all. The route is wrong under option (b) as well, since a clarify case must call nothing while COURSE_UNLOCKS declares a data capability — so ROUTER_BUG holds under both readings.


## Measurements behind the classifications

All three classifying agents re-ran the routing under **three different revisions** of
`advisor_intent.py` that existed during the session (883, 783 and 704 lines). **All 50
questions classify identically in every one**, so no row here depends on which revision
was on disk.

### Splitting `TIMETABLE_DATA` out of `PLANNER_DATA`

The owner's five-way domain taxonomy separates `TIMETABLE_DATA` from `PLANNER_DATA`.
`_DOMAIN` in `advisor_intent.py` currently has three coarse domains and is **load-bearing**:
`classify_intent` returns `MIXED` when a question's families span more than one.

Measured cost of splitting it **in the code**: exactly **one** question changes.

> **TT28** «أكّد، تجاهل جدولي الحالي وابنِ واحدًا جديدًا» → `PLANNER_REBUILD` becomes `MIXED`.
> The same two words feed both families — «تجاهل»+«جدولي» is the rebuild marker, «جدولي»+«الحالي»
> the current-timetable one. Both are `planner` today, so precedence picks `REBUILD`. Split
> them and precedence is lost.

TT28 is the «أكّد» case — the incident this branch exists for. Near-misses that do *not*
flip: TT14 (`TIMETABLE_CLASH` + `CURRENT_TIMETABLE`, both move together), TT02 and TT27
(wholly planner), TT08 (already `MIXED`).

**Conclusion: the five-way taxonomy belongs in a separate `POLICY_DOMAIN_FOR_FAMILY` map
used only for policy gating. Do not repurpose `_DOMAIN`, whose job is deciding `MIXED`.**
This matches the owner's own split — domain for gating, family for capability narrowing.

### Domain distribution over all 50

| Domain | Count |
|---|---|
| `COURSE_DATA` | 21 |
| `PLANNER_DATA` | 21 |
| `TIMETABLE_DATA` | 7 |
| `POLICY` | 1 |
| `GENERAL` | 0 |

### `CAPABILITY_FOR_FAMILY` is advisory today

Nothing narrows the advertised tool surface at this commit — a family is a label, and
`GENERAL_AGENT` advertises the full role-filtered registry, a strict superset of every
family's tools. So "different family" is **not** automatically safe and superset is **not**
the test. The test used throughout is the owner's sharper one: *does the surface contain a
tool that can actually answer this question?* That is what separates TT12/TT13 (yes —
`my_timetable` returns `registered_course_count`, `registered_credit_hours`,
`courses_without_a_time`) from TT10 (no — `build_my_timetable` cannot see a draft, so the
superset contains only a fabricating tool).

Enforcement is commit 7B.

### Policy-gate state at this commit

Commit 6B is paused, so the broad gate is still in force. Measured over the 50:

    data questions the policy gate would refuse:  CP11, CP15   (both COURSE_PRIORITY)

Both are now classified into a **data family** by commit 6A, which is precisely what 6B's
`DATA_INTENTS` rule needs in order to reach them. Before 6A, CP11 was `GENERAL_AGENT` and
the rule would have missed it — which is why the router work had to come first.

## What 6A.2 will change

Only these five:

| Case | Gap |
|---|---|
| TT10 | `_EDIT_WORD` covers عدلت/غيرت/حذفت/اضفت but not the past-tense **«اخترت»**. The deterministic `EDIT_DRAFT` route exists and never fires. |
| CP02 | Cross-course ranking routed to `COURSE_UNLOCKS` → `why_course_locked`, which analyses one named course and cannot rank a plan. |
| CP14 | — see reason above |
| CP16 | — see reason above |
| CP20 | Same direction defect as CP02. |

Everything else is a specification repair, an accepted alias, or a correct abstention.

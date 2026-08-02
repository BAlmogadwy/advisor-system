<!-- Reconnaissance deliverable for feat/advisor-capability-screens.
     Produced by tracing the code, not by reading documentation. Every claim carries
     file:line. Nothing here is implemented — this exists so the four student screens
     can be built as thin adapters over services that already exist, and so the places
     where the backend does NOT support a proposed field are known before an endpoint
     promises one. -->

# Capability Map — student-facing advisor screens
Branch `feat/advisor-capability-screens` · synthesised from five parallel code traces.

---

## 1. Capability-to-service map

| Student feature | Registered capability | Underlying implementation (file:line) | Student-reachable? | Already used by chat? | State |
|---|---|---|---|---|---|
| Current timetable | `my_timetable` — reg. `core/services/virtual_advisor_capabilities.py:2073-2096`, exec `:1049` | `core/services/student_sections.py:96` `get_student_term_baseline` (+ `student_gender`, non-strict, `vac.py:1075-1082`) | YES (`_ALL_ROLES`, `:2093`) | YES (agent loop `core/services/virtual_advisor.py:1586`) | Registered |
| Locked-course reason | `why_course_locked` — reg. `vac.py:2016-2043`, exec `:917` | `core/services/student_unlock.py:34` `build_unlock_report` (reason kinds `:149`, `:158-160`, `:162`, `:164`) | YES (`:2040`) | YES | Registered |
| Prerequisites | `course_prerequisites` — reg. `vac.py:1860-1885`, exec `:480` | `core/services/student_helpers.py:28` `get_prerequisites`; elective branch `vac.py:441` `_resolve_elective_slot` | YES (`:1882`) | YES | Registered |
| Clash-free sections | `my_clash_free_sections` — reg. `vac.py:2152-2185`, exec `:1324`, shared setup `:1262` | `core/services/planner_builder.py:87` `_overlap` + `:196` `_catalog_for_courses`; baseline from `student_sections.py:96` | YES (`:2182`) | YES | Registered |
| Course eligibility | `course_eligibility` — reg. `vac.py:1887-1908`, exec `:560` | `core/services/eligibility.py:98` `build_course_eligibility_report` | **NO** — `allowed_roles=_PROGRAM_ROLES` (`:1905`), and `_PROGRAM_ROLES` (`:47`) is SUPER_ADMIN + GENERAL_ADVISOR only (plain ADVISOR excluded too) | YES, for those two staff roles | Registered (staff-only, correctly) |
| Elective alternatives | **none** | Read resolver: `vac.py:441` `_resolve_elective_slot` (private, unregistered). Second, independent resolver: `core/services/reporting.py:178` `resolve_elective_recommendations` (collapses to ONE pick, `:272`) | Only as a side-effect of asking `course_prerequisites` about a placeholder code (`vac.py:515-529`) | Indirectly | **Unmapped** |
| Conflict details (student's own registered meetings) | **none** | No baseline-vs-baseline computation exists anywhere. Nearest: `my_clash_free_sections` conflicts = candidate-vs-baseline (`vac.py:1369-1371`); `core/services/conflict_matrix.py:12` is a staff **co-enrolment demand** matrix, `@role_required(ROLE_ADVISOR)` at `core/report_views.py:1504`/`:1544`; gap minutes exist at `core/services/planner_builder.py:154` but are consumed only inside the solver objective (`:515`, `:714`) | NO | **NOT FOUND** |
| Plan by term | `my_plan_by_term` — reg. `vac.py:2098-2127`, exec `:1140` | `core/report_views.py:231` `_build_student_plan_payload` (imported into the service layer at `vac.py:1149`) | YES (`:2124`) | YES | Registered |

**Two false friends worth naming.** (a) `core/services/elective_resolver.py` is *not* the elective resolver a screen wants: its `resolve_elective_placeholders` (`:139`) is a write path that flips `StudentCourse.status` to "studying" (`:269-273`), called only from `core/management/commands/scrape_students.py:298`, untested, and structurally unregistrable — `AdvisorCapabilityRegistry.register` rejects any `read_only=False` capability (`vac.py:211-215`). (b) `conflict_matrix` is co-enrolment counts over *recommended* courses; it never reads `TermSectionMeeting`, days or times.

---

## 2. Current output shapes

### 2.1 Timetable — `my_timetable` (`vac.py:1121-1137`)

```
student_id, academic_year, term,
meetings[]      -> exactly 7 keys: day, start, end, course_code, section, room, instructor   (:1083-1095)
registrations[] -> course_code, section, credits, meeting_count, scheduled                   (:1101-1113)
registered_course_count, registered_credit_hours, courses_without_a_time, note
+ registry-injected: tool, ok  (:249-250)
```
- `meetings` silently truncated at `_MAX_LIST_ROWS * 2` = 40 (`:1125`, `_MAX_LIST_ROWS = 20` at `:50`) with **no** omitted-counter (contrast `_exec_find_students`, which does emit `students_omitted`, `:326`).
- **Absent:** `building`, `floor_wing` (both columns exist on `core/models.py:400-401`, dropped by `student_sections.py:138-157`), lecture/lab type, online flag, `conflicts`, `gaps`, `course_name`, `term_section_id`.
- Key renaming happens at the executor boundary: the service emits `start_time`/`end_time` (`student_sections.py:150-151`), the capability emits `start`/`end`.
- Source rows are **one per meeting** (`student_sections.py:135-157`); the de-duplicated `registrations` list exists precisely because summing credits over `meetings` multi-counts (documented at `vac.py:1097-1100`, "measured at 36 credits for a student actually carrying 14").
- Term may be silently re-pointed to the single published `(year, term)` when the configured one is empty (`:1063-1073`); the payload echoes what it actually used.

### 2.2 Clash-free sections — `my_clash_free_sections` (`vac.py:1398-1412`)

```
ok, student_id, compared_against_term ("<year>/<term>", one STRING), courses[], note, tool
courses[]      -> course_code, sections_on_file, clash_free[], clashing[], status         (:1388-1396)
status          in {OK, ALL_CLASH, NOT_ON_FILE}                                           (:1361, :1394)
clash_free[]   -> section, meetings[]  where meetings are FORMATTED STRINGS "DAY HH:MM-HH:MM" (:1379-1382)
clashing[]     -> the same + conflicts[] capped at 4                                       (:1384)
conflicts[]    -> section_meeting, conflicts_with ("<course> <section>"), registered_meeting (:1372-1378)
failure shape  -> {ok: False, error, reason: "COHORT_UNRESOLVED"}                          (:1297-1301)
```
- `clash_free`/`clashing` truncated at 20 (`:1392-1393`) with **no** truncation flag; `sections_on_file` (`:1391`) is the only signal, and it is the *post*-gender-filter count.
- No seat/capacity fields — correct: 0 of 718 global `term_sections` rows carry `available_capacity` or `registered_count` (verified live), which is also why `consider_capacity=False` is passed as a "dead lever" at `vac.py:1540`.
- **Contradiction adjudicated.** The timetable reader called this "the ONLY student-reachable overlap output in the system". The sections reader is right that `build_my_timetable` also derives clashes and exposes them as `reason_code: ALL_SECTIONS_CLASH` (`vac.py:1420-1438`). Both are student-reachable, and they use *different* overlap implementations (exact minutes vs 5-minute bitmask, see §4).

### 2.3 Prerequisites / locked-course

`course_prerequisites` returns **three mutually exclusive shapes**:
```
placeholder : ok, course_code, is_elective_placeholder=True, options[], note, tool   (:517-529)  -- NO per_program key
not-in-plan : ok, course_code, per_program=[], note "Course not found in any programme plan."  (:531-537)
normal      : ok, course_code, per_program[]{program, prerequisites, course_name,
                                             programme_term, credit_hours}           (:539-557)
```
`prerequisites` here is the **raw** list from `get_prerequisites` and still contains the pseudo-code `"146(HOURS)"` unsplit.

`why_course_locked` returns **five** shapes sharing a base of `student_id, course_code, unlocks_directly[], unlocks_directly_count` (`:951-959`):
```
open_now : + status, name, fits_this_term, explanation        (:962-968)
passed   : + status, name, explanation                        (:971)
studying : + status, name, explanation                        (:974-979)
blocked  : + status, name, steps_away, opens_n_courses,
             nearest_course_you_can_take_now, blocked_by[], explanation  (:982-995)
error    : {ok: False, error: "<CODE> is not in this student's degree plan."}  (:996)
```
`blocked_by[]` is a **heterogeneous union keyed on `kind`**, built in `student_unlock.py`:
`MISSING_COURSE {kind, code, name, own_status}` (`:158-160`) · `MISSING_HOURS {kind, required, earned, registered, effective, met, remaining}` — no `code` (`:162`, merged from `eligibility.py:47-54`) · `UNKNOWN_PREREQ {kind, code}` (`:149`) · `ASK_ADVISOR {kind}` (`:164`). The last two have **no test coverage**.

`my_plan_by_term` (`:1178-1192`): `ok, student_id, program, summary, terms, blocker_hints, note, tool`; per-course keys from `core/report_views.py:279-289` — `course_code, type, programme_term, credit_hours, status, can_register, prerequisites, missing_prereqs, importance_score` (verified live: `{"course_code":"FE1","type":"Free Elective","programme_term":6,"credit_hours":2,"status":"passed","can_register":false,"prerequisites":[],"missing_prereqs":[]}`). The empty-level branch (`:1167-1176`) instead emits `plan_level` and **omits** `summary` and `blocker_hints`. `blocker_hints` = `[{course_code, blocks, unlock_score}]` (`report_views.py:313-329`). The inner shape of `summary` is **UNVERIFIED** — no reader quoted its keys.

### 2.4 Electives — `_resolve_elective_slot` (`vac.py:441`)

```
returns None  -> NOT an elective slot (no matching ProgrammeRequirement, or type lacks "elective")  (:454-455)
returns list  -> each option EXACTLY: course_code, course_name, credit_hours, prerequisites[]        (:470-475)
                 sorted by code, truncated to _MAX_COURSE_MATCHES = 10                               (:51, :477)
```
Verified live: `AI/AI1` → 1 option, `IS/IS1` → 5, `CS/CS1` → 3, but `AI/FE1`, `AI/GSE1`, `AI/AI2` → `is_elective_placeholder: True` with `"options": []`. **Absent:** requirement-group name, required credits, completed credits, term availability, `can_register`, status.

`my_progress` reduces the same data to bare code strings — `"elective_slots": [c["code"] …]` (`:898`) — discarding name/credits/term/type that `build_unlock_report` had carried (`student_unlock.py:130`).

**UNVERIFIED across all readers:** the inner key set of `policy_lookup`'s `policies` entries, and `get_student_context`'s `credit_policy_evidence` sub-dict.

---

## 3. Permission and identity findings

**Registry gate.** `AdvisorCapabilityRegistry.execute` (`vac.py:225-251`) checks the role twice — schema-listing (`:218-220`) and execution (`:236-237`, "This tool is not allowed for your role.") — never raises (`:240-248`), and setdefaults `tool` and `ok` (`:249-250`). `_scope_role` (`:57-65`) returns `""` for an absent scope and deliberately does **not** default to SUPER_ADMIN.

**Student-reachable set = 13, not 14.** *Readers contradict here.* The identity reader's summary says "fourteen carry `_ALL_ROLES`", but its own per-capability enumeration of `allowed_roles` lists exactly 13: `get_student_context` (`:1802`), `lookup_course` (`:1825`), `recommend_courses` (`:1855`), `course_prerequisites` (`:1882`), `my_progress` (`:2011`), `why_course_locked` (`:2040`), `graduation_progress` (`:2068`), `my_timetable` (`:2093`), `my_plan_by_term` (`:2124`), `my_advisor` (`:2147`), `my_clash_free_sections` (`:2182`), `build_my_timetable` (`:2224`), `policy_lookup` (`:2274`). The electives reader independently enumerated the registry live at 18 capabilities, 5 of which are staff/program-gated (`find_students`, `course_eligibility`, `graduation_shortfall`, `portfolio_triage`, `aggregate_demand`). 18 − 5 = 13. **Adjudicated: 13**, on the enumerated `allowed_roles` evidence.

**The clamp.** `_resolve_scoped_student_id` (`vac.py:76-140`): for ROLE_STUDENT (`:97-107`) the id is forced from `scope["student_id"]`, and any mismatched `student_id` argument returns `"Students can only access their own records."` (`:106`); unlinked returns `"No student identity is linked to this session."` (`:104`); the fall-through is `"This request carries no authority to read a student record."` (`:140`). ADVISOR is clamped to portfolio (`:120-124`), GENERAL_ADVISOR to departments (`:126-130`). A second, independent clamp is the cohort filter: `my_clash_free_sections` and `build_my_timetable` refuse with `COHORT_UNRESOLVED` (`:1300`, `:1486`) when `student_gender_strict` cannot resolve, rather than letting an empty gender become an all-pass filter. **`my_timetable` is the asymmetric one** — it uses the lenient `student_gender` (`:1053`, `:1075`) plus a post-hoc row filter (`:1076-1082`) and degrades silently where its siblings refuse. Both readers who traced it agree; it is undocumented and should be decided and commented either way.

**Critical for endpoint design: `registry.execute` still takes a `scope` dict, not an `AdvisorPrincipal`.** Signature at `vac.py:225-232` is `execute(self, name, args, *, scope: dict | None = None, ctx: dict | None = None)`; that module does not import `advisor_principal` at all. `AdvisorPrincipal` lives one layer up (`core/services/advisor_principal.py:55-134`) and converts itself with `as_scope()` (`:118-134`) — STUDENT yields exactly `{"role", "student_id"}` (`:128`); STAFF yields `{"role", "advisor_id", "departments", "student_id": None}` (`:129-134`), i.e. staff scope never carries a subject id. It fails closed by raising `IdentityError` (`:92`, `:109`, `:145-151`). **Therefore a new endpoint must do `registry.execute(name, args, scope=AdvisorPrincipal.for_student(request).as_scope(), ctx=…)` and must never assemble a scope dict by hand.** The precedent for getting this wrong is already in-tree: `_seed_policy_evidence` passes a hand-written `scope={"role": ROLE_STUDENT}` with no `student_id` (`core/services/virtual_advisor.py:1372`).

**No HTTP route reaches the registry.** The only `registry.execute` call sites in the repo are `core/services/virtual_advisor.py:1371` and `:1586`, plus `evals/` and `tests/`. No view imports `virtual_advisor_capabilities`. One student-reachable JSON endpoint duplicates a capability's service while bypassing the registry entirely — `recommend_view` (`core/api_views.py:25-58`, route `core/urls.py:343`) calls `recommend_next_courses` directly, guarded only by `require_student_scope` (`core/services/policy.py:129`), returning a bare list of codes with none of the `credit_policy` evidence the capability adds. Its three tests all log in as SUPER_ADMIN (`tests/test_recommend_endpoint.py:16-60`), so the student path is untested. **This is the precedent not to follow.**

**Reusable plumbing is private.** `_principal` (`core/advisor_conversation_views.py:60-70`), `_forbidden` (`:101-102`) and `_over_budget` (`:105-123`) are module-private; nothing else imports them. The ownership idiom that goes with them matters: ownership belongs **in the query** (`:88`, `:742`, `:666`) so cross-student access 404s, never 403s.

**Rate limiting.** `core/services/rate_limit.py:38-42` declares exactly five budget names; `consume` does `max_calls, window_seconds = LIMITS[budget]` at `:84` with no `.get` — an unregistered name raises `KeyError`. `HISTORY` (240 calls / 600 s, `:66`) is the only read-shaped budget and is documented as a runaway-script backstop; `GENERATION` (6/600 s, `:50`) is sized for a 90-second model call and must not be charged for a query that calls no model.

---

## 4. Genuine gaps (integration only)

### A. The logic exists but is not student-reachable

| # | Missing | Evidence | Work |
|---|---|---|---|
| A1 | **No HTTP surface for any capability.** A student can reach all 13 only by persuading the LLM to call them mid-chat. | Only `registry.execute` sites: `virtual_advisor.py:1371`, `:1586`. `core/urls.py` routes nothing to a capability. | **ADAPTER** — a thin view resolving `AdvisorPrincipal.for_student(request)`. No academic logic. |
| A2 | **Elective options resolver is unregistered.** `_resolve_elective_slot` is a genuine placeholder→options resolver reachable only as a side-effect of `course_prerequisites`. | `vac.py:441`; 18 registered capabilities, none elective-named. It is already read-only, so it passes the registry's `read_only` guard (`:211-215`). | **REGISTRATION** (of the existing service). |
| A3 | **Gap minutes are computed but never surfaced.** 3,718 of 3,807 students have >0 gap minutes (mean 551.5 min/week). | `planner_builder.py:154` `_gap_minutes_from_meetings`, consumed only at `:515` and `:714` inside the solver objective. `evals/advisor/expected.yaml:4160-4178` explicitly classifies blanket gap-refusal as WRONG and the gap as **wiring**, not data. | **ADAPTER** — the function already takes `list[Meeting]` and merges per-day intervals. |
| A4 | **`building`/`floor_wing` dropped in transit.** 1,668 of 1,668 student-reachable meetings carry both, non-empty. | Columns `core/models.py:400-401`; dropped at `student_sections.py:138-157`; absent at `vac.py:1083-1095`. | **ADAPTER** — but note it invalidates eval item 30 (`expected.yaml:845-870`), whose abstention rests on the field being absent; that label must be changed in the same commit. |
| A5 | **Per-student eligibility reason computed then discarded.** | `eligibility.py:144-151`, `:179-186` build `blocked_samples`; `:201` returns it; `vac.py:593-602` copies seven keys and omits it. Staff-only surface. | **ADAPTER** (staff screens only). |
| A6 | **The prerequisite graph is discarded by `my_progress`.** A dependents view therefore costs one `why_course_locked` call per course. | `student_unlock.py:230-240` returns `graph`; `vac.py:882-914` never reads it; only `:941-948` mines it. | **ADAPTER**. |
| A7 | **Truncation is silent on the clash-free lists.** Fires on real data — CS492 has 57 global sections. | `vac.py:1392-1393` vs siblings that do flag it (`:724-726`, `:799-801`). | **ADAPTER** (add a flag, or raise the cap only on the screen path — the cap protects the chat token budget). |
| A8 | `course_eligibility` is unreachable for STUDENT *and* for plain ADVISOR. | `vac.py:1905` + `_PROGRAM_ROLES` at `:47`; asserted absent for students at `tests/test_virtual_advisor_agent_loop.py:200`; evals treat it as a refusal probe (`evals/advisor/scope_student_only.py:49-53`). | **NOTHING NEEDED** — correct behaviour. Widening it would leak cohort counts and a 15-id sample of other students. Build student screens from `why_course_locked` + `course_prerequisites` instead. |

### B. The logic does not exist

| # | Missing | Evidence | Work |
|---|---|---|---|
| B1 | **Self-conflict detection over a student's own registered meetings.** No function anywhere pairs two baseline meetings. | `vac.py:1336-1386` — `mine` is built once from `c["baseline"]` and every `_overlap` call pairs one *candidate* with one *baseline* meeting; `planner_builder.py:243-249` treats the baseline as `occupied` only when `keep_registered`; `my_timetable` has no `conflicts` key. | **ADAPTER** over existing primitives (`planner_builder._overlap`). But measured: only **2 of 3,807** students have a genuine self-overlap between two different (course, section) pairs. A conflict screen will be empty for 99.95% of students — gaps (A3) are where the content is. |
| B2 | **Candidate-vs-candidate clash within one `my_clash_free_sections` call.** Two sections of two different courses can both be returned `clash_free` and still collide. | `vac.py:1336-1347` never appends to `mine`; `:1350-1396` compares each course only against the baseline. Contrast `planner_builder._get_conflict_pairs:365-396` and `_choose:342-348`, which do build the joint problem. | **ADAPTER** — call once per course and state the limitation, or delegate multi-course questions to `build_my_timetable`. |
| B3 | **Same-course self-collision is mislabelled.** Asking "which CS323 sections fit" while registered in CS323/M1 yields `conflicts_with: "CS323 M1"`. | `vac.py:1336-1347` applies no filter against the queried codes; label built at `:1343`. No test covers it. | **ADAPTER** — one condition. |
| B4 | **Elective requirement GROUP (required vs completed credits) does not exist.** | Only two elective models, `core/models.py:174` and `:204`; `ProgrammeRequirement.credit_hours` (`:83-91`) is per-slot; `type` is free text, not a FK to any group. | **ADAPTER** by aggregation over the free-text `type` — but this is derived, not stored. Say so on the screen. |
| B5 | **Lecture/lab type, and lecture-lab pairing.** | No type column on either model (live `PRAGMA table_info(term_section_meetings)` = id/day/start/end/building/floor_wing/room/instructor/timestamps/term_section_id); repo-wide grep for `linked_section\|parent_section\|lab_section` hits only `docs/PR4-DOR.md`. The only classifier is a ≥80-minute duration heuristic behind a default-OFF flag (`core/services/timetable_lab_predicate.py:45`, `:48`), which would label 547 of 1,668 meetings "lab" with no ground truth. | **NOTHING NEEDED** — showing it would be a fabricated field. A screen must not promise lecture-lab pairing. |
| B6 | **Online flag is not a field.** It exists only as magic strings: `building='أونلاين'` + `room='Blackboard'` on 55 of 1,668 meetings (3.3%). The boolean `ProgrammeRequirement.is_online` is course-level and staff-path only (`core/services/timetable_online.py:23`, `:67`). | Both columns are plain `TextField(blank=True, default="")` with no constraint. | **ADAPTER with a caveat** — if exposed, label it "inferred from the room name", never assert it. |
| B7 | **Seat/capacity data.** 0 of 718 global sections carry `available_capacity` or `registered_count`. | The code already knows: `vac.py:1540` marks `consider_capacity` a "dead lever"; the notes at `:1408-1409` and `:1594-1595` forbid saying a section has room. | **NOTHING NEEDED** — a constraint on the screen, not missing work. |
| B8 | **"Not offered this term" as a lock reason.** The nearest signal, `fits_this_term`, is plan-term *parity arithmetic*, not an offering feed. | `core/services/recommender.py:105` `if c["term"] % 2 != next_term_parity`, with the term inferred from the id prefix (`:16-24`); surfaced at `student_unlock.py:141` and `vac.py:966`. The real signal lives elsewhere: `NOT_ON_FILE` at `vac.py:1362` and `:1420-1424`. | **NOTHING NEEDED** as logic — but the screen must **not** render `fits_this_term` as "offered this term". |
| B9 | **Consistency between the four "prerequisites satisfied" implementations.** `recommender.prereqs_ok` (`:92-93`) never splits `N(HOURS)` — grep for `HOURS\|split_hour\|hour_gate` in that file returns nothing — so every hour-gated course is excluded there even when `student_unlock.py:74` has judged the gate met. | Four sites: `report_views.py:268-277`, `student_unlock.py:71-74`, `eligibility.py:124-173` (a fifth inline copy of the hour regex at `:129-139`, using last-match where `split_hour_prereqs:26` uses `max`), `recommender.py:92-93`. Untested at `tests/test_student_unlock.py:84-88`. | **ADAPTER/remediation, separate task.** Until fixed, a card showing `status: open_now` beside `fits_this_term: false` is self-contradicting. |
| B10 | **Consistency between the three overlap implementations.** | (1) exact minutes `planner_builder.py:87-94`; (2) 5-minute floor-divided bitmask `:101-116` (`_SLOT_MINUTES = 5` at `:50`) used by `_bitmask_build_option_b:460-462` and `_cp_build_option:637-651`, where a sub-bucket meeting collapses to a zero mask (`:114-116`); (3) `group_availability.py:64-66` with different day/validity guards. Day normalisation is also asymmetric *within* `_exec_my_clash_free_sections`: baseline side `vac.py:1339` applies `.upper()[:3]`, catalogue side `planner_builder.py:188` does not. `DAY_MAP`'s five Arabic keys are committed mojibake (`planner_builder.py:17-21`). Latent only — every live `day` value is `SUN/MON/TUE/WED/THU`. | **NOTHING NEEDED for a screen** beyond *consuming one and saying which*. Reconciling them is separate remediation; do not smuggle it into screen work. |
| B11 | **Four disagreeing "is this an elective placeholder" schemes.** | (1) `vac.py:454` type-contains-"elective" (the documented-correct one, `:442-446`); (2) `student_unlock.py:27-31` GS/GSE/FE prefix + `len(code) <= 4`; (3) `elective_resolver.py:37-64` hardcoded tuple; (4) `db_admin_views.py:728-731` exact `type="Program Elective"` — which under-reports by 46 of 84 rows despite its own docstring at `:719-722`. | **ADAPTER** — pick scheme (1). |

### C. Data, not code (owner's, flagged so it is not mistaken for a bug)

77 of 84 live elective placeholder slots have **zero** `ElectiveTermMapping` rows, so `_resolve_elective_slot` returns `"options": []` for 92% of real placeholders. All 16 Free Elective and all 30 University Elective slots are unmapped, plus 31 of 38 Program Elective slots — broader than the memory note "FE1/FE2/GSE1 have no ElectiveTermMapping". Separately, the 5 IS rows in `ElectiveCourse` carry `programme=''`, which breaks `eligibility.py:71`'s exact-match lookup (though not `_resolve_elective_slot`, which joins on `elective_id` at `vac.py:463`).

### D. Coverage holes (not gaps; do not mistake for proven behaviour)

`_weekly_grid`/`_weekly_timetable` (`core/student_auth_views.py:173`, `:197`) have no test — *readers differ in emphasis here*: the timetable reader found none, the identity reader cited `tests/test_student_login.py:136`; adjudicated — that test covers login/isolation, not grid rendering. Also uncovered: the empty-options elective path (the 92% case), `reporting.py:271`'s silent drop, `student_unlock.py:27` `_is_placeholder`, `UNKNOWN_PREREQ`/`ASK_ADVISOR`, `resolve_elective_placeholders` entirely, the five elective admin endpoints, `build_course_eligibility_report`'s real logic (both its tests monkeypatch it away), and `recommend_view` as a student.

---

## 5. Recommended API contract per screen — DESCRIBED, NOT IMPLEMENTED

Common to all four: the adapter must (i) normalise the envelope, because four student capabilities (`my_progress`, `why_course_locked`, `graduation_progress`, `my_timetable`) return no explicit `ok` and rely on `execute`'s setdefault at `vac.py:249-250`; (ii) **strip or rewrite `note`/`contact_note` and `tool`** — those strings are instructions aimed at the model, not text for a human (`vac.py:910-914`, `:1038-1045` "it must never be reported as 'you are graduating'", `:1130-1137`, `:1185-1191`, `:1403-1410`, `:1592-1601`, `:522-527`, `:1707-1723`); (iii) **accept no `student_id` at all** — the clamp tolerates one, but the safe contract omits the field, as the existing student screens already do (`core/student_auth_views.py:223`, `:261`, `:300`, `:327`); (iv) echo the term actually rendered, since three code paths pick it three different ways (`vac.py:1063-1073` vs `student_auth_views.py:362-378` vs `virtual_advisor.py:940-947` — they agree today only because all live data is 1447/term 2, one combo, 18,468 rows).

**Screen 1 — Timetable.** FROM `my_timetable`. Convert: rename `start`/`end` back to explicit structured times (they were `start_time`/`end_time` upstream); add `building`/`floor_wing` by plumbing them through `student_sections.py:138-157` (100% populated, and re-label the eval); pick a grain per panel — grid from `meetings`, credits **only** from `registrations` (`:1101-1118`); replace the silent 40-row cap with an explicit `meetings_omitted`; render `courses_without_a_time` as a visible section, not a footnote. **Where the brief's suggested contract diverges from the backend, follow the backend:** do not promise a lecture/lab type or an `is_online` boolean (B5, B6), and emit **no** seat or availability figure (B7). Conflicts and gaps must be *computed by the adapter* from the meetings already returned — they are not in the payload.

**Screen 2 — Clash-free sections.** FROM `my_clash_free_sections`. Convert: parse the pre-formatted `"DAY HH:MM-HH:MM"` strings (`:1381`, `:1374-1376`) back into structured `{day, start, end}` so the UI can sort and grid them — the structured `Meeting` objects exist upstream and are thrown away at the executor boundary; add truncation flags (A7); filter the baseline by the queried codes so a same-course hit is not labelled as a foreign collision (B3); state explicitly that multiple courses were **not** checked against each other (B2), or route multi-course requests to `build_my_timetable`, whose `unplaced` entries already carry the proper `reason_code` enum (`:1420-1450`) that this capability lacks. Preserve the `NOT_ON_FILE` ≠ "not available" distinction as a UI affordance (pinned by `tests/test_virtual_advisor_current_registrations.py:757-758`) and word the list as "your cohort's sections", since `sections_on_file` is the post-gender-filter count.

**Screen 3 — Locked course / prerequisites.** FROM **two** calls merged: `why_course_locked` (personal verdict) + `course_prerequisites` (formal structure). Neither returns the other's data. Convert: collapse the five polymorphic branches into one envelope where only `student_id`, `course_code`, `unlocks_directly`, `unlocks_directly_count` are guaranteed; tag `blocked_by` as a **discriminated union on `kind`** and handle **four** kinds, not two (`UNKNOWN_PREREQ` and `ASK_ADVISOR` are emittable and untested); never render `"146(HOURS)"` as a course — `student_unlock.py:71` splits it, `course_prerequisites` does not, so the same field name `prerequisites` means different things on the two sources; special-case "not in this student's degree plan", which is an **error** in one capability (`vac.py:996`), a soft note in another (`:531-537`), and silence in a third (`eligibility.py:115-116`). Suppress `fits_this_term` or gate it, per B8/B9.

**Screen 4 — Electives.** FROM `_resolve_elective_slot` (register it first, A2). Convert: the current return type collapses two distinct states into one — `None` = "not an elective slot" (`:455`) vs `[]` = "elective slot, no mapping published" (`:462`, `:477`), and the caller's `if elective_options is not None:` (`:516`) cannot express the difference. **The empty state is the common case (92%)**, so it needs an explicit reason code, not a blank list. Also add year/term arguments: this resolver filters on neither (`:458-460`), while the sibling resolver does (`reporting.py:215-219`) — masked today only because every mapping row is 1448/term 1. Requirement-group name must be derived from the free-text `type`; required-vs-completed credits must be aggregated (B4) — neither exists. **Where the brief assumes a field, follow the backend:** term availability and per-option `can_register` are not returned and would be new joins. Finally, note the two resolvers disagree by design — `_resolve_elective_slot` returns all options (≤10), `resolve_elective_recommendations` collapses to one load-balanced pick (`reporting.py:272`) and silently drops the requirement when nothing is eligible (`:271`, no `else`). A screen showing options will diverge from what the chat advisor says today; that is the correct direction, but say it.

---

## 6. Frontend rule

**No academic rule may be reimplemented in JavaScript.** The browser renders server-computed verdicts; it never derives them. Specifically:

- **Meeting overlap / clash detection** — authoritative: `core/services/planner_builder.py:87` `_overlap` (exact half-open minute intervals, malformed times fail open). Two other implementations exist (`planner_builder.py:101-116` 5-minute bitmask; `core/services/group_availability.py:64-66`) and are not guaranteed to agree; a screen consumes one and says which. JS must not compare `"HH:MM"` strings.
- **Prerequisite satisfaction, including the credit-hour gate** — authoritative: `core/services/student_unlock.py:34` `build_unlock_report` (open rule at `:71-74`), with `core/services/eligibility.py:14-29` `split_hour_prereqs` and `:32-54` `hour_gate`. JS must never parse the `"146(HOURS)"` pseudo-code or compute `earned + registered ≥ required`.
- **Elective placeholder detection and option resolution** — authoritative: `core/services/virtual_advisor_capabilities.py:441` `_resolve_elective_slot`, detecting via `ProgrammeRequirement.type` (`:454`). JS must not sniff `GS`/`GSE`/`FE` prefixes or code length — that heuristic is one of four disagreeing schemes (`student_unlock.py:27-31`).
- **Credit thresholds and credit policy** — authoritative: `credit_policy_evidence` as wired at `vac.py:615` (`recommend_courses`), and the registered-credit de-duplication at `vac.py:1097-1118`. Summing `credits` over `meetings` in JS reproduces the documented multi-count bug (36 vs 14).
- **Cohort (gender) filtering** — authoritative: `core/services/student_sections.py` `student_gender_strict` / `gender_section_filter`. Never client-side, and never an all-pass fallback.
- **Seats/availability** — no client-side inference of any kind; 0 of 718 global sections carry capacity data.

The existing shared renderer `static/js/prereq-graph.js` (used by both `core/templates/core/student_courses.html:216` and the staff dashboard) is the correct pattern: it lays out a graph the server already computed. Keep it rendering-only.

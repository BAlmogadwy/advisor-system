<!-- Reconnaissance deliverable for feat/advisor-capability-screens.
     The product decision is to expose the EXISTING builder at /planner/ to students:
     no new scheduling algorithm, no gap analysis, no room or building analysis, no
     lecture-vs-lab labels, no online inference, no separate clash-free-sections screen.
     Nothing here is implemented. Every claim carries file:line. -->

# STUDENT PLANNER INTEGRATION BRIEF
**App:** `core/` · **Target:** expose the EXISTING `/planner/` builder to students
**Sources:** four parallel readers (permissions, contract, state, frontend). Every line number below that I quote as fact was re-opened and confirmed against the working tree; claims I could not re-open are marked *(reader-cited, unverified)*.

---

## 1. What already works

### Behaviour 1 — pick courses: WORKS (server side)
`planner_build_view` accepts a caller-supplied `shortlist` and normalises each item to 8 keys; `course_code` is the only required one (`core/planner_views.py:532-568`, empty-code rejection at `:539-542`). `build_plans` derives its course set from that list (`core/services/planner_builder.py:1150-1157`).

### Behaviour 2 — pin a section: WORKS, and it is a per-course pin
`core/planner_views.py:543-553` normalises `pinned_sections` to `{term_section_id, section}`. `core/services/planner_builder.py:1162-1178` filters *that one course's* catalog to the pinned ids. Because the filter runs on the shared `catalog` dict **before** any solver runs (`:1157` → `:1162-1178` → `_run_method` at `:1205-1239`), the pin is honoured identically by all three methods.

### Behaviour 3 — leave sections open: WORKS, and it is the default
A shortlist item with no `pinned_sections` hits `continue` at `core/services/planner_builder.py:1164-1165` and keeps its full option list. **Mixed pinning is therefore native: pin one course, leave the other five open, in a single request.** This is the single most important negative finding of this brief and it is negative in the good direction — see §2.

### Behaviour 4 — generate several alternatives: WORKS, up to 9
`core/services/planner_builder.py:1308-1312` — three methods (`A` CP-SAT, `B`/`C` bitmask DFS) × top-3 each. Variants come from BFS over exclusion sets (`_top_k_method`, `:1241-1276`): solve, record chosen `term_section_id`s (`_sig`, `:1196-1203`), re-solve with each banned (`:1267-1272`). Each option carries `mappings` with per-course section + `{day, start_time, end_time}` meetings (`fmt_option`, `:1278-1306`).

### Behaviour 5 — compare: PARTIALLY WORKS
The raw material is in the response: every option returns its full meeting list (`:1298-1301`), so days-on-campus / earliest start / latest end are derivable **without re-solving**. `_day_count_from_mask` (`:126`) and `_gap_minutes_from_meetings` (`:154`) already exist in the same module.

### Behaviour 6 — choose: PARTIALLY WORKS
An option is fully identified by its `term_section_id` list (`:1297`), and the JS already harvests exactly that to persist a choice *(reader-cited: `static/js/page-planner.js:664`)*.

### Behaviour 7 — honest failure explanation: PARTIALLY WORKS (per-course only)
Per-course reasons are real and specific: `"Blocked by prerequisites: …"` (`:301, :426, :614, :819`), `"No sections available"` (`:310, :435, :623, :828`), `"Model infeasible under current hard constraints"` (`:1084`).

### Behaviour 10 — principal-derived scope: THE PRIMITIVE EXISTS AND IS ALREADY WIRED
`require_student_scope` has a correct, audited STUDENT branch — identity from the session, never from the request (`core/services/policy.py:147-166`, comment at `:148-149`, deny `STUDENT_SCOPE_SELF_ONLY` at `:155`). It is **already called** by planner context (`core/planner_views.py:151-153`) and planner build (`:527-529`). Separately, `AdvisorPrincipal.for_student` (`core/services/advisor_principal.py:84-93`) fails closed on non-students and `as_scope()` (`:118-124`) emits the scope dict the capability layer consumes.

### The decisive precedent: a student-scoped adapter over this exact builder already ships
`build_my_timetable` in `core/services/virtual_advisor_capabilities.py` calls **the service, never the view**, for a student:
- identity clamped to self: `_resolve_scoped_student_id` (`:76-106`, STUDENT branch `:97-106`)
- cohort resolved **strictly**, refusing rather than guessing: `:1483-1486`
- baseline read **server-side**: `:1529`
- credits derived from `ProgrammeRequirement`, not the client: `:1492-1497`
- `build_plans(...)` at `:1532-1543`, with the dead levers deliberately disabled (`consider_capacity=False` with the comment "dead lever: available_capacity is NULL on every row", `:1540`)
- docstring at `:1458-1460` states the design rule outright: the HTTP view is staff-only and throttled, so the student path wires the service.

Measured performance of that path: **0.13s for 9 options** (`docs/ADVISOR-CAPABILITY-SWEEP.md`, "`build_plans` is real and fast (0.13s, 9 options)").

### Server-side rendering precedent
A student weekly grid already renders with **zero JavaScript**: `_weekly_grid` at `core/student_auth_views.py:197-203` returns `{slots, rows, columns}`, consumed by `core/templates/core/student_home.html` *(reader-cited: `:66-95`)*.

---

## 2. What is missing, and of what kind

### FIRST, THE NEGATIVE CHECK THE BRIEF ASKED FOR
**The builder CAN pin one section while leaving every other course fully open.** I re-opened `core/services/planner_builder.py:1162-1178`: the pin loop iterates shortlist items, `continue`s (`:1164-1165`) on any item without `pinned_sections`, and replaces `catalog[code]` only for pinned courses. There is no global "pinned mode" and no coupling between courses. **This is not a blocker, and no scheduling work is needed for behaviours 1–4.**

### NOT SUPPORTED BY THE BUILDER (genuine builder limits — none of them block the first slice)

1. **A pin is a filter, not an assertion — the builder cannot tell an invalid pin from an empty course.** Pinning an id that is not in the (gender-filtered) catalog yields `catalog[code] = []` (`:1173-1178`), which the solvers report as `"No sections available"` (`:310, :435, :623, :828`) — byte-identical to "this course has no sections at all." *(Contract reader; confirmed structurally.)* Any student-facing pin UI must validate the pin against the catalog **before** calling the builder, because the builder will never say "your pin was the problem."
2. **A pin does not force the course to be scheduled.** Unless `must_take` or `strict_per_course`, the relaxed model may drop the pinned course (`:891, :894` — `if strict_per_course or is_must:` gates the `== 1` constraint).
3. **You cannot ask for N alternatives, and you cannot reproduce a run.** `k=3` is a default on an inner helper (`:1242`) invoked by a hardcoded call site (`:1310`); `build_plans`'s signature (`:1138-1149`) has neither a count nor a seed parameter, and the solvers randomise (`:1054` region sets an 8s cap per solve; the readers additionally cite `randomize_search`/`random_seed` and `random.shuffle` — *reader-cited*).
4. **Cross-method duplicates are structural.** `seen` is created inside `_top_k_method` (`:1245`) and the loop calls it once per method (`:1309-1310`), so A1/B1/C1 can be the identical timetable. The comparable key `_sig` already exists (`:1196-1203`) — deduping is post-processing, not solver work.

### PERMISSION
- **Every planner route denies students.** `_require_staff` admits only `{SUPER_ADMIN, GENERAL_ADVISOR, ADVISOR}` (`core/planner_views.py:69-73`); called at `:107` (page), `:122` (context), `:405` (catalog), `:491` (build), and `:339` *(reader-cited: save)*. A student's `require_student_scope` branch is never reached because the staff floor short-circuits above it.
- **`planner_sections_catalog_view` has no ownership check at all.** I read `:402-484` end to end: it reads `student_id` at `:415`, uses it only for `student_gender_strict` at `:438`, and **never calls `require_student_scope`**. Today that is a staff-to-staff exposure; the moment the role gate opens it becomes student-to-student. Blocker.
- **`student_id` is optional on the build path** (`:523`): omit it and both the scope check and the cohort filter are skipped entirely (`:522-530`), because both live inside the `if student_id:`. For a student caller the id must be mandatory *and* session-derived.
- **Choosing must not write registrations.** The save path deletes and recreates the student's whole `StudentTermSection` set for the term (`core/services/student_sections.py:244` / `.delete()` at `:258`) — the same table the planner reads back as "currently registered." A "plan and compare" feature must not touch it.

### ADAPTER
- **Cohort resolution disagrees with itself.** Catalog uses `student_gender_strict` and returns 409 `STUDENT_COHORT_UNRESOLVED` (`core/planner_views.py:438-440`); build uses the non-strict `student_gender` (`:530`), which returns `Q()` — an all-pass across both cohorts — when the Student row is missing (`core/services/student_sections.py:52`). For a student build this is a segregation leak dressed as a fallback. The correct behaviour already ships in the capability layer (`virtual_advisor_capabilities.py:1483-1486`).
- **Baseline is client-supplied.** `baseline = payload.get("baseline", [])` (`core/planner_views.py:503`) is passed straight to `build_plans` (`:580`) with only an `isinstance(..., list)` guard. `get_student_term_baseline` is already imported and used in this very module (`:202`).
- **Eligibility and credits are client-supplied.** `status`, `missing_prerequisites`, `credits` are taken verbatim (`:560-565`) and trusted as truth by the solvers *(reader-cited: `planner_builder.py:295-305, :421-429, :814-823`)*; `credits_map` is built only from them (`:1181-1186`). A student client could assert `status:"Eligible"` on a blocked course or `credits:0` to evade the cap. Derivation exists server-side in the capability layer (`:1492-1497`).
- **Plan-level feasibility is a constant.** `"best_feasible": True` is a literal (`:1338`), and `_top_k_method` guarantees a non-empty result even on total failure (`:1274-1276`), so a hopeless request returns 3+ options reading `0/n` under a "best feasible plan found" banner. The verdict is derivable in the adapter from `best["scheduled"]` vs `target` (`:1314-1318`) without touching a solver.
- **Comparison metrics.** Derive days / earliest / latest from the returned `meetings` (`:1298-1301`).
- **Display fields.** `course_name` is loaded into the catalog and then dropped by `fmt_option` (`:1291-1304`); `course_number` is always `""`; `available_capacity`/`registered_count` never reach the response *(the catalog endpoint does return them, `core/planner_views.py:461-462`)*. Note the capability layer's finding that capacity is NULL on every planner-slice row (`:1540`) — do not build UI on it.
- **Free-text reasons.** Ten English strings, no enum. A partial prefix translator already exists *(reader-cited: `virtual_advisor_capabilities.py:1420-1450`, falling through to `"OTHER"`)*. Note the contract reader's site list for `"No sections available"` is **incomplete** — it cites `:310, :435, :827` and misses `:623`; the true sites are `:310, :435, :623, :828`.

### UI
- **No navigation.** The whole staff nav is inside `{% if role != "STUDENT" %}` (`core/templates/core/partials/sidebar.html:23`, with the explicit comment at `:20-22` that every target "would 403"); `/planner/` sits at `:64-67`.
- **Behaviour 8 — add courses from other screens: NOTHING EXISTS.** A grep of `core/templates/` for `/planner/` returns exactly one hit — the sidebar link at `:64`. No student screen offers "add to planner."
- **Behaviour 9 — open from chat pre-populated: NOT SUPPORTED BY THE URL CONTRACT.** The deep-link block (`static/js/page-planner.js:960-993`) reads only `student`/`student_id`/`sid`, `year`, `term` — **there is no course-list parameter** — then auto-clicks fetch after 120ms (`:990-992`). `static/js/page-student-advisor.js` contains no reference to the planner at all. So "open from chat with these courses" needs a new entry contract, not a reuse.
- **The HTML route denies in JSON.** `planner_page` returns the JSON error body with `content_type="application/json"` (`core/planner_views.py:107-109`) — a student hitting the URL sees a raw blob, not a page.
- **Staff chrome and identity input** — see §4.

### TEST
No test hits any planner URL; the only planner tests call `build_plans` directly *(reader-cited: `tests/test_planner_builder.py`, three unit tests, one of which passes an empty shortlist)*. All four readers independently confirmed this by grep. There is nothing to extend — only new tests to write.

---

## 3. Security findings

**The conversation endpoints fixed this exact defect class, and the planner has not.** `core/advisor_conversation_views.py` derives the principal from the session only (`_principal`, `:60-70`, docstring: "A request that names a student id is describing what it wants, not who it is") and folds ownership **into the query filter** (`_owned_conversation`, `:73-88`: "Fetching by id and checking `.student_id` afterwards would be one forgotten line away from a leak"). `core/services/advisor_principal.py` exists specifically to kill the "student_id parameter AND a scope dict" ambiguity (module docstring `:4`).

**BLOCKERS — student-reachable the moment the role gate opens:**

1. **`planner_sections_catalog_view` has no scope check.** `core/planner_views.py:402-484`: `_require_staff` at `:405`, `student_id` read at `:415`, `student_gender_strict` at `:438`, `gender_section_filter` at `:441` — and `require_student_scope` appears nowhere in the function. Student A could name student B's id and both enumerate B's cohort catalogue and learn B's cohort from the 409/200 split. **This must be fixed before any student can reach the endpoint, not after.**
2. **Non-strict cohort resolution on the build path.** `:530` uses `student_gender`, which returns an all-pass `Q()` for a missing Student row (`core/services/student_sections.py:52`). 722 of 3,807 `StudentTermSection` ids have no Student row (`core/planner_views.py:432-435`, in-code comment). A student in that set gets the **other cohort's** sections scheduled, silently. This is a segregation failure, not a UX nit.
3. **Payload-supplied identity everywhere.** All five endpoints take `student_id` from the JSON body (`:131`, `:415`, `:504`, and *reader-cited* `:347`), and the UI feeds it from a free-text box plus a URL parameter (`static/js/page-planner.js:967`). Three of five endpoints re-check it correctly; two do not (catalog: never; build: only when supplied).

**BLOCKER for the jobs subsystem, but NOT student-reachable:**

4. **`PlannerJob` poll / result / cancel have no owner filter.** Confirmed: `get_planner_job` is `PlannerJob.objects.filter(id=job_id).first()` (`core/services/planner_job_runner.py:149-150`) and `cancel_planner_job` repeats it at `:165` — while **declaring a `user` kwarg at `:156` that the body never reads**. `submitted_by` is populated at `:142` and then never used for authorisation. Any General Advisor can read or cancel any other's job. This is precisely the defect the conversation endpoints eliminated. **Students cannot reach it today** (the routes sit behind a GENERAL_ADVISOR floor and are scenario-based, not student-based), so for this project it is a *do-not-reuse* verdict plus an owner-filter fix on its own ticket — not a student blocker.

**Write paths a student must not inherit:**

5. `planner_save_student_sections_view` destroys and rewrites the student's registration rows (`core/services/student_sections.py:244`, `.delete()` at `:258`); the JS hardcodes `confirm_replace:true` *(reader-cited: `page-planner.js:669`)*, so the confirmation flag is not a real gate.
6. **Merely fetching context can write.** `core/planner_views.py:229-233` calls `replace_student_term_sections(..., source="auto_from_studying")` when the baseline is empty. A "read my plan" action must not mutate registrations on behalf of a student.

**Not a finding:** gender filtering *is* derived server-side and never trusted from the client on both catalog (`:436-441`) and build (`:518-530`) — the defect is which helper build uses, not where the value comes from.

---

## 4. Reuse verdict on the existing UI

### Scheduling logic DOES live in JavaScript — confirmed, and it is not marginal
Verified sites in `static/js/page-planner.js`:
- prerequisite eligibility and "may this be added": `:354` (`prereqOk`), `:368` (`canAdd`), gating the Add button at `:385`/`:391`/`:394`
- section status classification, including the `Full+Conflict` composite: `:528`
- baseline clash detection: `hasConflict` at `:484`
- credit total: `credits += Number(c.credits||0)` at `:252`
- "which option is best": max-`scheduled` scan at `:757-758`, auto-selected

The frontend reader additionally cites day-name normalisation, minute parsing, the overlap double-loop, and grid geometry in `static/js/shared-timetable.js` *(reader-cited)*. The direction of trust is backwards throughout: the browser decides eligibility, the server accepts the browser's verdict (`core/planner_views.py:560-563`).

### What is staff-only chrome that must not ship
Student ID / year / term inputs and the deep-link auto-fetch (`page-planner.js:960-993`); the role chip; Keep-vs-Ignore-Registered; Swaps; the mapping-quality trust strip; Simplified-mode toggle; **Apply**; Strict; Ignore capacity; the Baseline/Overlay visual-source selector; "Advanced diagnostics" (which is where the *only* honest failure text currently renders); the importance-score filters; and the option rationale strings that name the algorithms ("Bitmask DFS lexicographic") *(all reader-cited from `core/templates/core/planner.html` and `page-planner.js`; the readers agree line-for-line)*. Two of these are load-bearing and must move server-side rather than simply vanish: **max credits** (a hard constraint, `core/planner_views.py:574`) and **ignore capacity** (`:573`).

### Verdict: **NEW THIN STUDENT TEMPLATE — calling a new student-scoped view over the SERVICE, not the staff endpoints as they stand.**

Justification, from the evidence rather than taste:
- "Refine the existing template behind a student-scoped view" would inherit eight JavaScript decisions that a student's answer must not depend on (`:252, :354, :368, :484, :528, :757-758` + shared-timetable). Deleting the staff chrome does not delete those; they are the same code paths that render the student-usable parts.
- The staff endpoints are the wrong *shape*, not merely the wrong *audience*: client baseline (`:503`→`:580`), client eligibility and credits (`:560-565`), a scope-less catalog (`:402-484`), and an optional student id (`:523`). Widening `_require_staff` would ship all four to students at once.
- The counter-pull — "don't rebuild the builder" — is fully satisfied without the template: `build_plans` is reusable **as a service**, and there is a working, reviewed precedent doing exactly that for a student principal (`virtual_advisor_capabilities.py:1458-1543`), measured at 0.13s for 9 options.
- The rendering pattern to copy already exists with zero JS: `_weekly_grid` (`core/student_auth_views.py:197-203`) feeding a server-rendered grid in `student_home.html`.

So: keep `build_plans` untouched; write a thin student view that derives identity, baseline, cohort, credits and eligibility server-side, calls the service, and hands the template pre-decided strings.

---

## 5. The smallest honest first slice

Pick courses → generate alternatives → compare → choose. Nothing else.

1. **PERMISSION** — A student-scoped planner view whose subject is the session principal (`AdvisorPrincipal.for_student`, `core/services/advisor_principal.py:84-93`) or `require_student_scope`'s STUDENT branch (`core/services/policy.py:147-166`). No `student_id` accepted from the body. Do **not** widen `_require_staff` (`core/planner_views.py:69-73`), and do **not** route through `planner_job_views` (scenario-keyed, single-worker, no owner filter — `planner_job_runner.py:149-150`).
2. **PERMISSION** — Strict cohort resolution on the student path: `student_gender_strict` with an explicit refusal, mirroring `core/planner_views.py:438-440` and `virtual_advisor_capabilities.py:1483-1486`. Never the all-pass `Q()` at `student_sections.py:52`.
3. **ADAPTER** — Server-derived inputs: baseline via `get_student_term_baseline` (already used at `core/planner_views.py:202`), credits via `ProgrammeRequirement` (pattern at `virtual_advisor_capabilities.py:1492-1497`), eligibility derived, not accepted. Ignore any client-sent `baseline` / `status` / `credits`.
4. **ADAPTER** — Call `build_plans` unchanged with the student's course list; leave every course unpinned in this slice (pinning is slice 2 — it is supported but needs pin validation, per §2 NOT-SUPPORTED item 1).
5. **ADAPTER** — Post-process the returned `options` before they leave the server: dedupe on `_sig` (`planner_builder.py:1196-1203`) across all nine, compute a real feasibility verdict from `scheduled` vs `target` (`:1314-1318`) instead of the constant at `:1338`, and attach days / earliest / latest per option from the returned `meetings` (`:1298-1301`).
6. **ADAPTER** — Map the free-text `unscheduled[].reason` values to stable codes plus Arabic text, covering at minimum `:301`-family (prereqs), `:310/:435/:623/:828` (no sections) and `:1084` (infeasible), with an explicit "unknown reason" bucket rather than leaking English.
7. **UI** — New thin template: course picker, N option cards showing the comparison facts from item 5, a "choose" that stores the chosen `term_section_id` list **without** touching `StudentTermSection`, and the failure explanation as the *primary* result when nothing schedules — not inside a `<details>`. Server-rendered grid on the `_weekly_grid` pattern (`core/student_auth_views.py:197-203`). No student-id input, no URL-supplied identity.
8. **UI** — One nav entry inside the student branch of `core/templates/core/partials/sidebar.html` (after `:161`), and an HTML denial for the page route instead of the JSON body at `core/planner_views.py:107-109`.
9. **TEST** — The boundary tests that do not exist anywhere today: a STUDENT client is denied `/planner/` and all five `ops/planner/*`; a STUDENT calling the new view for another id gets 403 (`STUDENT_SCOPE_SELF_ONLY`); a student with an unresolvable cohort gets a refusal, not the other cohort's sections; a client-sent `baseline`/`credits`/`status` is ignored; nothing in `StudentTermSection` changes across a full pick→build→choose cycle. Mutation-check each.
10. **PERMISSION** — Same-slice fix to `planner_sections_catalog_view`: add the missing `require_student_scope` (`core/planner_views.py:402-484`) even if the student path does not call it, because it is the one endpoint whose exposure would be a cross-student leak by default.

**Explicitly out of this slice:** pinning, applying/registering, swap suggestions (placeholder strings, `planner_builder.py:1320-1330`), capacity display (NULL on every row), room/instructor, entry from chat or other screens, async jobs.

---

## 6. Risks

1. **Opening the role gate without fixing the catalog endpoint leaks student data on day one.** `planner_sections_catalog_view` reads a body-supplied `student_id` (`core/planner_views.py:415`) and has no scope check anywhere in `:402-484`. Concrete failure: student A posts student B's id, gets B's cohort-filtered catalogue and — from the 409 `STUDENT_COHORT_UNRESOLVED` vs 200 split (`:438-440`) — learns whether B has a Student row and which cohort B is in.

2. **The non-strict cohort helper puts a student in the wrong-gender timetable and calls it success.** `student_gender` at `:530` → `Q()` at `student_sections.py:52` when the Student row is missing (722 of 3,807 ids, per the in-code note at `:432-435`). Concrete failure: a build returns a complete, clash-free, entirely opposite-cohort timetable with no warning, and the catalog screen for the same student — which *does* refuse (`:438-440`) — disagrees with it.

3. **Build latency: two credible numbers, and the pessimistic one exceeds the request timeout.** Measured: **0.13s for 9 options** on real data (`docs/ADVISOR-CAPABILITY-SWEEP.md`). Structural bound: `_top_k_method` pops are bounded by 1 + k·|sig| (`:1249-1272`), each pop is a full solve, and method A caps each solve at 8s (`:1054`) — so ~19 solves ≈ 152s for a 6-course shortlist, against a `--timeout 120` gunicorn worker *(reader-cited: `Procfile`, `render.yaml`)*; methods B and C are DFS with **no** time limit. **Adjudication: both readers are right about different things** — the contract reader measured the common case, the state reader bounded the tail. Concrete failure: a pathological shortlist kills the worker mid-request and the student sees a blank 502 with no message at all, because the work is synchronous (`:576-589`) and the async machinery is not reusable (scenario-keyed, `max_workers=1`). Mitigation is a wall-clock budget across the A/B/C loop, not a job queue.

4. **The throttle is thinner than it reads, and students iterate more than advisors.** `@throttle(max_calls=5, window_seconds=60)` (`:489`) over a **per-process in-memory dict** *(reader-cited: `core/authz.py:50`)* with 2 workers — so the real cap is 5–10/min depending on which worker answers, and it resets on deploy. Concrete failure: a student adding one course at a time hits 429 during normal exploration, while a determined caller doubles their budget by chance of routing. (A durable bucket exists on this branch — `core/services/rate_limit.py` — but is wired only to the advisor endpoints.)

5. **"Choose" quietly becoming "register."** The only per-student persistence in this area is the destructive `replace_student_term_sections` (`student_sections.py:244`, `.delete()` at `:258`), and the planner already writes through it on a *read* path (`core/planner_views.py:229-233`). Concrete failure: a student clicks "choose this plan," their real registration rows for the term are deleted and replaced by an unapproved wishlist, and the next context fetch reports the wishlist back as "currently registered" — with no advisor in the loop and no undo.

**Adjudications on reader disagreements (all resolved against the file, not the readers):**
- `seen` in `_top_k_method` is at **`:1245`** (frontend reader's `:1243` is the signature's closing line); the options loop is **`:1308-1312`** (frontend's `:1304-1308` is off by four). Contract and state readers are correct; the substantive claim — dedup is per-method — holds either way.
- `_require_staff` call sites are **`:107, :122, :339, :405, :491`** (state reader's `:123` and `:406` are one line late).
- `"No sections available"` has **four** sites (`:310, :435, :623, :828`); the contract reader's enumeration lists three and omits `:623`. Treat the ten-string reason inventory as a floor, not a closed set — which is itself an argument for an explicit "unknown reason" bucket in item 6 of §5.
- No reader claimed the builder cannot mix pinned and open courses; I re-checked because it was nominated as the potential headline. It can (`:1162-1178`).

# Course guidance — reconnaissance and contracts

Three deterministic student surfaces: **why a course is locked**, **what a course
formally requires**, and **what can fill an elective slot**.

Reconnaissance only. No implementation, no planner changes, no CI changes.

## Correction: the first draft of this document was wrong

It opened by naming the failure mode — *the risk is not missing logic, it is a
second implementation of logic that already has one* — then did reconnaissance on
the service layer and the capability layer and **none on the screen layer**, and
proposed rebuilding a page that already ships. It also left "enumerate the reason
kinds" as an open question that `docs/CAPABILITY-SCREEN-MAP.md:83`, written
earlier on this same branch's history, had already answered.

Both errors have the same cause: recon that did not read the recon that existed.
What follows starts from `CAPABILITY-SCREEN-MAP.md` and records only the delta.

---

## 1. Screen 1 already exists

`core/student_auth_views.py:218` `student_courses_view` calls
`student_unlock.build_unlock_report` — the intended "spine" — and renders
`core/templates/core/student_courses.html`, linked from the student sidebar
(`partials/sidebar.html:168`). It is **server-rendered, in Arabic, with no
JavaScript for the locked-course content**.

Every field the first draft proposed is already on it:

| Proposed | Already rendered |
|---|---|
| `counts` | KPI row — and it also has `one_step`, which the draft dropped |
| `most_useful_to_pass` | `:42` «أكثر خطوة مفيدة الآن: اجتياز {code} … يفتح لك {n}» |
| `steps_away` | `:115`, guarded `{% if c.steps %}` |
| `opens_n_courses` | `:116` |
| `reasons[].text_ar` | `:122–139`, discriminated on `kind` |
| `nearest_you_can_take_now` | `:142` anchor to `#c-{code}` |

So the honest scope for the locked-course surface is **not a new screen**. It is:

1. **Tests for what the page RENDERS.** A second correction: the first rewrite of
   this section said the page had no coverage at all. Wrong.
   `tests/test_student_unlock.py` has 24 tests, including
   `test_screen_renders_from_session_identity_only:154`, which proves the identity
   clamp (`?student_id=` cannot change whose report is built) and that staff are
   redirected off it.

   What is untested is the **rendering of the reason branches**. The only
   assertion touching a kind is `:98` on the service (`kinds == ["MISSING_HOURS"]`);
   nothing asserts that the template's `MISSING_HOURS` branch shows
   `effective / required / remaining`, that `UNKNOWN_PREREQ` shows its Arabic
   sentence, or that the `{% else %}` degrades rather than echoing a token. Those
   branches are the product.
2. **Extension**, if a review of the page finds something missing.

Converting it to JSON + a client renderer would also reverse a stated project
preference: `docs/PLANNER-STUDENT-INTEGRATION.md:50` records zero-JavaScript
server rendering as a virtue, and `CAPABILITY-SCREEN-MAP.md:186` names
`prereq-graph.js` as the correct pattern — laying out a graph the server computed.

---

## 2. The reason vocabulary is four kinds, and their shapes differ

Not an open question. `core/services/student_unlock.py` emits exactly:

| kind | payload | line |
|---|---|---|
| `MISSING_COURSE` | `code, name, own_status` | `:159` |
| `MISSING_HOURS` | `required, earned, registered, effective, met, remaining` — **no `code`** | `:162` |
| `UNKNOWN_PREREQ` | `code` — a code **not in the student's plan** | `:149` |
| `ASK_ADVISOR` | nothing | `:164` |

**This decides the contract shape, so it cannot be deferred.** A flat
`{code, text_ar, course_code}` reason object cannot carry three of the four:
`MISSING_HOURS` has no course and its entire value is the numbers
(`effective/required/remaining` — «تبقّى {n} ساعة»), `ASK_ADVISOR` has no payload,
and `UNKNOWN_PREREQ`'s code is by definition unresolvable to a plan course, so
neither a name nor the adjacent `nearest_open` logic applies.

Any reason contract must be a **discriminated union on `kind`, with four members**.
The shipping template already is one (`:122–139`), including an `{% else %}` that
degrades to «راجع مرشدك الأكاديمي» rather than echoing a token — which is the
correct handling of the `_translate_unplaced` defect class, already implemented.

**How often each fires, across all 320 students** — this is the number that
matters, and one student's report was not it:

| kind | students affected |
|---|---|
| `MISSING_COURSE` | 320 |
| `MISSING_HOURS` | 168 (52%) |
| `UNKNOWN_PREREQ` | **74 (23%)** |
| `ASK_ADVISOR` | 0 today |

`UNKNOWN_PREREQ` firing on a quarter of students matters because the chat path
already reaches `why()`'s fall-through for it
(`virtual_advisor_capabilities.py:897`) and hands the student the literal token
`unknown_prereq`. The defect this document warns about is not hypothetical; it is
live, in the capability, now.

`ASK_ADVISOR` is unreachable only by data accident: it needs a status outside
`passed`/`studying`/`not_taken`, and `_build_student_plan_payload` emits only
those three — but `StudentCourse.status` is a `TextField` with **no choices**
(`core/models.py:61`), so one bad write reaches it. Handle it; do not rely on it
being impossible.

**Two nullable fields the first draft declared as plain values:** `steps` is
`None` when `hours_only`, and `steps_to` returns `None` on a cycle
(`student_unlock.py:124`, `:169`); `nearest_open` is `None` when nothing on the
chain is open (`:178`). The template guards both. A contract must declare both.

---

## 3. Cost, measured

`build_unlock_report(4401603, 1448, 1)`: **0.019–0.029 s, 127 queries**, returning
11 open / 2 locked / 5 elective slots / 30 passed / 2 studying, and a graph of 34
edges. Live reason kinds observed: `MISSING_COURSE` only — which is why a sample
cannot substitute for reading the code.

127 is near the FLOOR, not the typical case. Across all 320 students: **min 118,
p50 135, p90 148, max 158**. It does not scale with plan size — it scales with how
much of the plan is unfinished (`corr(queries, not_taken) = +0.98`), so the cost is
highest for the students in most trouble and lowest for near-graduates. Quoting a
single number from one well-advanced student was the mistake; quoting the spread is
the fix.

**119 of the 127 are one statement** — `SELECT prerequisite_course_code FROM
prerequisites WHERE …` — an N+1 issued twice over the same plan:
`report_views.py:268` inside `_build_student_plan_payload`, then
`student_unlock.py:71` again, discarding the `prerequisites` / `missing_prereqs` /
`can_register` keys the payload already computed (`report_views.py:279`).

Still not a reason to optimise speculatively. It IS a reason not to call the report
twice on one journey: locked list → "why?" → course detail would do exactly that.
Pass the report along, do not re-derive it.

---

## 4. The elective surface is a data problem before it is a screen problem

`CAPABILITY-SCREEN-MAP.md:153`, measured: **77 of 84 live elective placeholder
slots have zero `ElectiveTermMapping` rows** — `_resolve_elective_slot` returns
`[]` for 92% of real placeholders. All 16 Free Elective and all 30 University
Elective slots are unmapped, plus 31 of 38 Program Elective slots. The
per-programme framing in the first draft ("8 of 12 programmes") hid this: even a
mapped programme has mostly empty slots.

`CAPABILITY-SCREEN-MAP.md:171` also already required the fix the first draft
regressed on: the resolver collapses `None` ("not an elective slot") and `[]`
("slot with nothing published") into one indistinguishable empty array. At 92%
the empty state IS the screen, so it needs an explicit reason code.

Sharper still, for the programmes that actually have students (AI, AI2, DS, DS2):
**26 of 28 `(programme, slot)` pairs resolve to zero options.** Only `AI/AI1` and
`DS/DS2` return anything, one course each. AI2 and DS2 — **115 of 320 students** —
get zero for every slot they have.

**And the data is not missing, the join is.** `ElectiveCourse` holds 55 rows
against only 23 `ElectiveTermMapping` rows, and `_resolve_elective_slot` reaches
electives *only* through the mapping table
(`virtual_advisor_capabilities.py:471`), so AI's 12 catalogued electives are
invisible to AI2 and AI3. A second path already reads the catalogue directly —
`eligibility._get_elective_prerequisites` (`eligibility.py:68`). That changes what
the screen should say: not "nothing exists", but "nothing is published for your
slot".

**Owner decision, not an implementation detail:** publish `ElectiveTermMapping`
rows first, or ship an expandable slot that mostly says «لم تُنشر خيارات هذه
المتطلب بعد». Do not ship a blank list either way.

---

## 4a. A live defect the recon surfaced: mandatory courses shown as elective slots

Two implementations of "is this an elective placeholder?" disagree, and one is in
the shipping screen:

- `student_unlock._is_placeholder(code, ctype)` (`:25`) — matches on **code shape**
  first (`GS`/`GSE`/`FE` prefix), only then consults the type.
- `virtual_advisor_capabilities.is_elective_slot(type)` (`:441`) — matches on the
  declared **`ProgrammeRequirement.type`**, and its docstring explains why: a code
  pattern "would miss new families".

Measured, they disagree on **seven real courses**, all declared `Mandatory`:

```
GS101 ISLAMIC STUDIES: BELIEF AND WORSHIP     12 programmes
GS103 ISLAMIC STUDIES: HUMAN RIGHTS            6
GS104 ISLAMIC STUDIES: ISLAMIC VALUES         12
GS111 ARABIC LANGUAGE SKILLS I                12
GS112 ARABIC LANGUAGE SKILLS II               12
GS151 UNIVERSITY LIFE SKILLS                   6
GS152 COMPUTER SKILLS                          6
```

`student_unlock.py:137` returns before the open/locked branch for anything it calls
a placeholder, so a mandatory course a student still needs appears in **neither
`open_courses` nor `locked_courses` nor any `counts` bucket** — and on screen it
reads as an elective slot with "choose with your adviser" rather than a course they
must pass.

Reference student 4401603: `elective_slots` is `['FE2', 'AI1', 'GS104', 'AI2',
'AI3']` — `GS104` is mandatory — and `counts` sums to **47 against a 50-row plan**.
Sampled across 60 students, `GS104` appears in 49 of them.

This is the same disease as issue #54 (section labels): one rule, two
implementations, one classifying by string shape and one by declared type.
**Tracked separately — it is a defect in shipped code, not a design question for
these screens**, and fixing it changes what the locked screen displays for most
students.

---

## 5. What has no authority behind it

A per-option `you_can_take_it_now` / `student_status` on elective options **is new
eligibility logic**, not a free field:

- `_resolve_elective_slot` returns four keys and compares nothing against the
  student; `prerequisites` is a raw split of `ElectiveCourse.prerequisites_csv`.
- `build_unlock_report` iterates the **plan**, which contains the placeholder
  `FE1` — never the concrete course that fills it — so a resolved elective has no
  status in it at all.
- `eligibility.py:71`'s exact-match lookup against `ElectiveCourse` is already
  documented broken for the 5 IS rows carrying `programme=''`
  (`CAPABILITY-SCREEN-MAP.md:153`).

Either cut the field or scope it as its own work with its own review. It must not
arrive inside a contract as though it were already computed.

---

## 6. Constraints, carried forward

Written down because each was learned expensively, mostly during the planner.

1. **No academic rule in JavaScript.** The strongest expression of this rule in
   the repo is a page with no JavaScript at all — see §1.
2. **No new engine.** And no new *field* whose engine does not exist — see §5.
3. **Closed vocabularies translated server-side**, never raw tokens. Four reason
   kinds, three `status` vocabularies (`your_status`, `student_status`, `kind`) —
   all of them, not just the one with an obvious enum.
4. **Ownership in the query.** 404, not 403. No endpoint accepts a student id.
5. **Arabic, from the server — including errors.** The planner review found *every*
   server error rendering in English on an Arabic page. A one-line constraint did
   not fix that; writing the strings did (`core/planner_draft_views.py:178–191`,
   the `UNPLACED_AR` dict, is the shape to copy). **Error bodies must be specified
   before the endpoint is written.**
   Note the trap in the first draft's own example: `course_name` comes from
   `Course.description` — uppercase English — and is the most-read string on the
   page.
6. **Arabic number agreement.** Six forms; `{n} + noun` is wrong for most. The
   shipping template sidesteps it with badge fragments («{n} خطوة») rather than
   sentences. That is an undocumented decision worth keeping deliberately.
7. **A programme discriminator is required.** `CS111` is two different courses
   across offset plans. `_exec_course_prerequisites` falls back to *every*
   programme carrying the code when the student's is blank, and
   `_resolve_elective_slot` then picks an arbitrary programme's electives from
   `.first()`. The planner met this and **refused** (`PlannerUnavailable`); these
   surfaces need the same refusal path, and must echo which programme they answered
   about.
8. **Echo the term rendered.** Three code paths choose it three different ways and
   agree today only because the live data is one combination
   (`CAPABILITY-SCREEN-MAP.md:163`).
9. **Do not inherit `_MAX_LIST_ROWS`.** `_exec_my_progress` truncates both
   `open_now` and `blocked` at 20 (`virtual_advisor_capabilities.py:914`, `:926`) —
   a chat-readability cap, like the elective one. **23 of 320 students have more
   than 20 locked courses** (max 28), so a screen lifting that shape would silently
   truncate exactly the list it exists to show, for the students with most to see.
10. **Say which "most useful course" you mean.** Two already exist in one call
   chain: `_build_student_plan_payload` returns `blocker_hints`
   (`report_views.py:294`) and `build_unlock_report` computes `top_blocker`
   independently (`student_unlock.py:197`), by a different key. For 4401603 they
   disagree in shape. A contract quoting one must name it.
11. **Read budgets.** No solver, no model. But `HISTORY` is currently the
   conversation-history budget — charging browsing to it lets a student paging
   through courses exhaust the allowance for re-reading their own adviser
   conversation. A separate name is one line; note `rate_limit.consume` indexes
   `LIMITS[budget]` without `.get`, so an unregistered name is a 500.
12. **Nothing here registers anything.**

---

## 7. Revised scope for this branch

1. **Test the shipping locked-course page** — `student_courses_view` and
   `student_courses.html`. It is the largest untested student-facing surface, and
   it is the thing the first draft proposed to rebuild.
2. **One course-detail surface**, not two. §1 of the first draft already recorded
   that elective options resolve *inside* `course_prerequisites`; they are one
   endpoint branching on placeholder-vs-course, and the `kind` enum needs a **third**
   value for "not in any programme plan" — a state
   `_exec_course_prerequisites:531` and `why_course_locked:996` currently disagree
   about (`CAPABILITY-SCREEN-MAP.md:169`).
3. **Decide the reason union and the Arabic strings before any endpoint.**
4. **Owner decision on electives** (§4) and on `you_can_take_it_now` (§5) before
   either is built.

## 8. Out of scope

- The planner lifecycle and CI (PR #53 owns both).
- Section labels and cohort classification (issue #54).
- Ranking or seat promises. **Correcting an inherited claim:** the first draft
  justified excluding seats with "`available_capacity` is NULL on every row". That
  is now false — measured, **50 of 50 live rows are non-NULL** (AI1→70, AI113→30,
  AI221→25). The claim came from a stale comment in shipped planner code
  (`student_planner.py:122`) that predates the 50-section XLSX import. Not
  promising seats may still be right, but it needs a current reason: capacity is a
  snapshot with no reservation behind it, and a screen that shows "25 seats" is
  read as "a seat for you". **The stale comment in `student_planner.py` should be
  corrected in PR #53, not here.**
- Ranking or difficulty. No difficulty data exists; `importance_score` is a staff
  planning number. Worth stating because it is the first thing a student asks once
  a slot shows five options.

**Deliberately reconsidered rather than excluded:** "what should I take next?" was
excluded in the first draft on the reasoning that `recommend_next_courses` already
exists — which is an argument *for* surfacing it, not against. The planner already
calls it for students (`planner_draft_views.py:246`), so today the planner names
courses and the guidance screens may not explain why those. That seam faces the
student and should be closed, not defended. Similarly, `graduation_progress` ships
at `student_auth_views.py:256` and is neither used nor excluded here.

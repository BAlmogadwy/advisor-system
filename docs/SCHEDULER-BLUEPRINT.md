# `scheduler` — a new timetabling subsystem

**Status:** design, not yet built. **Decision date:** 2026-07-26.
**Owner's brief:** *"new timetable subsystem but keep the current one untouched"*,
*"never relate it to the current one, never look at any saved scenario — we want
to build the best we can do."*

---

## 0. What this is, and what it is not

A **new, self-contained timetabling subsystem** living in its own Django app,
`scheduler/`. It shares the database *server* with the existing app and reads the
same institutional facts (students, programme plans, rooms, instructors), but it
shares **no code, no tables, and no state** with `core/services/timetable_*`.

**Hard boundaries, enforced by CI:**

| Rule | Why |
|---|---|
| `scheduler/` never imports from `core.services.timetable_*` | zero coupling — the current builder must remain untouched and unaffected |
| `scheduler/` **may** consume the upstream advising recommender (`recommender_batch`) and read-only institutional models | demand is academic policy, not scheduling policy — see §0.2. Two sources of advising truth would be a worse defect than any timetabling bug |
| `scheduler/` never reads `TimetableScenario`, `DeliveryBoard`, `SectionPlacement`, `TermSectionMeeting`, or any `scenario_*` table | clean room: no saved scenario is ever a baseline, warm start, or reference |
| `scheduler/` writes only to its own `sch_*` tables | the current system cannot be regressed by anything here |
| `core/` never imports from `scheduler/` | the old system stays independent |

The existing engine keeps running exactly as it does today. Nothing in this plan
modifies it.

### Reading institutional data is not "looking at a scenario"

A timetable cannot be built without students, courses, rooms and instructors.
Those are **institutional facts**. A *scenario* is a previously built timetable —
an output. This subsystem reads the former and never the latter.

### Demand comes from the recommender — we do NOT reinvent it

Demand is **already derived per run**, from institutional data, by
`core/services/recommender_batch.py :: batch_recommend`:

1. compute the student's real term from join year + current academic year/term;
2. filter by term parity (odd/even offering);
3. exclude courses already passed or currently being studied;
4. require all prerequisites satisfied;
5. rank by unlock-count, past-due priority, term order, GS de-prioritisation;
6. greedily fill to the 18-credit cap.

`scenario_student_course_requests` is a **materialised cache** of that
computation (`sync_scenario_student_course_requests`), not the source of truth.

**Design decision: the recommender is an upstream service that `scheduler`
consumes; it is not part of the timetable subsystem and is not rewritten.**

That boundary is deliberate. The recommender is the *advising* engine — the app's
primary domain, shared by reports, the conflict matrix and debug panels. It
encodes academic policy, not scheduling policy. Duplicating it would create **two
different answers to "what should this student take"**, which is a far worse
defect than anything in the timetable builder. `scheduler` therefore takes
per-student course demand as an **input**, exactly as it takes rooms and
instructors as inputs.

This does not breach the clean-room rule: the rule isolates `scheduler` from the
current **timetable builder/optimiser** (`core/services/timetable_*`), not from
the institution's advising engine.

> *Corrected 2026-07-26 — an earlier draft of this document claimed demand
> existed only as scenario artifacts and had to be re-derived from transcripts.
> That was wrong; the derivation exists and is sound.*

---

## 1. Why a new subsystem at all — the failure mode we must not repeat

Two greenfield attempts already exist in this project's history. Both produced
first-rate thinking and **neither ever placed a class in production**:

- the portable research engine (exact Benders rooming, proven optima, Hall
  witnesses) — its own production replay concluded *not production-ready*: the
  rebuild did not finish in 364 s, compaction made zero moves, and it could not
  represent real multi-board shared sections;
- the canonical constraint package — five registries, tri-state certification,
  and **zero engine stages calling it**. Deleting it broke nothing, which is the
  proof it was never load-bearing.

Both failed the same way: **they were layers, built beside the product, with no
caller and no forcing function.**

> **The governing rule of this design: every slice must be usable end-to-end by
> itself.** Never "build the layer now, wire it later." A slice that cannot be
> run from a command and produce a result a human can act on is not done.

---

## 2. What "professional" means here — the non-negotiables

Each one is a direct answer to a measured defect in the current engine.

| # | Principle | The defect it answers |
|---|---|---|
| N1 | **Immutable identity.** `OfferingId`, `SectionId`, `MeetingId` are opaque and stable. Display `course_code` is never an identifier. | `FE1` and `CS111` are each *two* offerings with different demand; joining on the display code silently merges them and corrupted the overlap matrix (250 code-level pairs vs 253 real). |
| N2 | **Sections are board-independent.** A section has one schedule; board/term membership is an explicit many-to-many, never a duplicated placement. | 22 of 48 shared sections are currently scheduled at *two different times at once* — physically impossible. |
| N3 | **Snapshot-based solving.** Input is extracted once into an immutable, fingerprinted `Snapshot`. The solver sees only that — no Django, no DB. | Today the 7-strategy generate performs ~8 full scenario builds *against live production tables*, using the DB as scratch space. |
| N4 | **Rooms and instructors are decision variables**, in the model from the first solve. | The current optimiser is room-blind by construction (`build_room_state_for_scenario` has zero callers; `rooms_by_id=None` at all four search sites; no room term in the objective) — so it *creates* boards that cannot be roomed. |
| N5 | **Compiled meeting requirements.** An offering declares its exact weekly multiset (kind, delivery mode, duration, count); credit hours are a default, not truth. | Meeting shape is currently guessed from credit hours + duration heuristics that disagree across surfaces (three different lab-duration literals). |
| N6 | **One rulebook.** Each constraint is defined once, with adapters for check / delta / native-CP-SAT. | The same rule had up to 8 divergent implementations; that drift is what the current engine spent two PR cycles undoing. |
| N7 | **Honest multi-objective.** Lexicographic only over *hard-feasibility* tiers; quality is an explicit weighted trade with published weights, and the engine can emit **alternatives** rather than one take-it-or-leave-it board. | The current 9-tuple collapses in practice — measured `(0,0,0,0,87060,56,32,52465,0)`: positions 0–3 and 8 never differentiate, so search is driven by one weighted sum. |
| N8 | **Reproducibility + provenance.** Same snapshot + same config + same seed ⇒ byte-identical board. Every run stamped with input/rules/config/code/seed fingerprints; runs with different fingerprints are not comparable. | Optimiser runs are routinely compared across code changes with no record of what produced them. |
| N9 | **Independent validation.** The checker shares the *specification* with the solver but none of its fast occupancy/delta code. | A solver that grades its own homework cannot detect its own modelling errors. |
| N10 | **Certificates, not verdicts.** "Infeasible" always carries the reason and a number — which resource, which bound, which witness. | Today a blocked board says "blocked"; the registrar cannot tell an unavoidable inventory shortage from a solver that gave up. Already proven valuable: the exact-rooming pass showed 8 meetings unroomable at fixed times, and a female cohort short **53 lecture-periods** — facts no solver can fix. |
| N11 | **Incremental evaluation.** Re-score only what a move changed. | One candidate evaluation currently costs **22 ms** (deepcopy + re-seat all 390 students + full rescore + a second quality pass); at ~10⁴ evaluations that is 3.7 minutes of pure scoring per click. |
| N12 | **Bounded everything.** Every search stage has a mandatory deadline; no unbounded default. | `TIMETABLE_CHAIN_TIME_LIMIT_SECONDS` still defaults to `0` = unbounded — the setting behind an 8.7-hour runaway job. |

---

## 3. Architecture

```
        institutional data (read-only: students, plans, transcript,
                            prerequisites, rooms, instructors, electives)
                                   │
                    ┌──────────────▼──────────────┐
                    │  intake/                    │   derive demand, plan sections,
                    │  → Snapshot (frozen, hashed)│   load supply, resolve the grid
                    └──────────────┬──────────────┘
                                   │   ← the ONLY thing the solver ever sees
        ┌──────────────────────────▼──────────────────────────┐
        │  solve/    construct → improve → exact repair        │
        │            rooms + instructors + students in-model   │
        └──────────────────────────┬──────────────────────────┘
                                   │
                    ┌──────────────▼──────────────┐
                    │  validate/  independent      │   no shared fast paths
                    │             checker          │   with solve/
                    └──────────────┬──────────────┘
                                   │
                    ┌──────────────▼──────────────┐
                    │  outcome/  candidates,       │   bounds, witnesses,
                    │            certificates      │   trade-off alternatives
                    └──────────────┬──────────────┘
                                   │   ← explicit human decision
                    ┌──────────────▼──────────────┐
                    │  apply/    atomic write to   │
                    │            sch_* tables      │
                    └─────────────────────────────┘
```

```
scheduler/
  domain/      pure Python, zero Django — ids, calendar/grid, entities, Snapshot
  intake/      demand derivation, section planning, supply, grid, fingerprinting
  rules/       one definition per constraint + check/delta/cpsat adapters
  solve/       constructive + improvement + exact repair
  validate/    independent checker
  outcome/     candidates, certificates, alternatives
  apply/       atomic persistence
  models.py    sch_* tables only
  management/commands/   every slice is runnable from the CLI
```

`domain/` importing Django is a CI failure. That single rule is what makes the
solver reproducible, unit-testable without a database, and impossible to
accidentally couple to the live DB.

---

## 4. Delivery slices

Each slice is independently runnable and produces something a human can act on.

### S1 — Snapshot + input readiness *(first deliverable)*
Take per-student demand from the upstream recommender, plan sections, load
rooms/instructors/grid, freeze and fingerprint a `Snapshot`. Emit a **readiness
report**: capacity deficits, missing instructor coverage, unmapped eligibility,
room-inventory shortfalls — *before* any solving.

`python manage.py sch_snapshot --year 1448 --term 1 --programs AI,DS`

Value on its own: answers "can a good timetable even exist from this data?" and
returns `READY` / `BLOCKED_INPUT` with numbers. Everything downstream depends on
it, and it is provable without a solver.

### S2 — Rulebook + independent validator
One definition per constraint; a checker that can grade any board. Runnable
against a snapshot + a proposed board.

### S3 — Constructive solve *(first real timetable)*
Rooms, instructors and students in-model from the start. Produces a complete
board with certificates. This is the slice that proves the architecture.

### S4 — Improvement + exact repair
Incremental evaluation (N11), bounded stages (N12), exact repair for the hard
residuals, alternatives for the registrar.

### S5 — Apply + UI
Atomic persistence into `sch_*`, its own workspace screen, exports.

---

## 5. What we deliberately reuse — *ideas*, not code

No code is copied from either previous attempt (one is deleted, the other is a
separate workspace). What we keep is the **knowledge**:

- exact whole-meeting room assignment with lexicographic capacity/change/waste,
  and the unavoidable-inventory vs congestion shortfall decomposition —
  independently re-derived and already proven in `timetable_exact_rooming`;
- profile-compressed student sectioning (identical demand sets collapse ~390
  students → ~108 profiles) as a real model, not ephemeral greedy seating;
- instructor working-day lower bounds `max(ceil(sessions/cap), distinct-day
  requirement)` so instructor quality is reported against a proven bound;
- Hall-deficiency / oversubscription witnesses as first-class certificates;
- `docs/TIMETABLE-CONSTRAINTS.md` — the institutional rules themselves. This is
  hard-won domain knowledge and the most valuable artifact the current system has.

---

## 6. Decisions taken by the owner (2026-07-26)

### D1 — A snapshot is single-gender. Always.
> *"we never generate one build for both M and F at the same time"*

Gender is a **property of the snapshot**, not a per-section attribute to be
reasoned about mid-solve. `Snapshot.gender ∈ {M, F}` is set at intake; the room
pool, the student population and the instructor pool are all filtered once, up
front. Nothing downstream carries a gender predicate.

This removes an entire class of constraint (H13) from the solver: a room is
either in the snapshot's pool or it does not exist. It also makes the two builds
trivially parallel and independently publishable.

### D2 — There is no prayer constraint. The grid is the authority.
> *"prayer time not exist anymore we mainly use the daily grid slots"*

`scheduler` models **no prayer rule**, and no implicit blackout. The declared
daily slot grid *is* the legal-time policy: a meeting is legal iff it occupies a
declared slot. Nothing else constrains time-of-day.

This is a simplification, not a loosening — the current system already enforces
prayer with no code at all (it holds only because the curated grid avoids those
windows), so making the grid the single explicit authority is strictly more
honest than an unenforced rule nobody checks. Time-of-day policy changes are
made by **editing the grid**, which is versioned and fingerprinted (N8).

### D3 — Section capacity comes from `programme_requirements.max_capacity`
…**where it is declared**, and from an explicit, reported policy where it is not.

Measured on live data: **362 of 596 requirement rows (61%) have `max_capacity`
NULL**; the 234 declared values range 5–60 with a mode of 25.

So the rule is:

```
capacity(offering)      = max_capacity                if declared
                        = policy default              otherwise   ← must be reported
sections(offering)      = ceil(demand / capacity)
```

Two things are load-bearing:

- the fallback is **declared configuration, never a magic number buried in code**
  (the current system hard-codes a `40` default in three places), and
- every offering that used the fallback is **named in the readiness report**, so a
  registrar sees exactly which capacities were assumed rather than approved.

This deliberately rejects the current system's `ceil(total_demand /
planned_sections)` basis, which is an *average* that conceals unequal section
sizes and reserves.

**Still open (needs a number, not a design):** the policy default itself. The mode
of the declared data is 25. `scheduler` will not invent one silently — S1 will run
with an explicit `--default-capacity` and report its impact.

---

### D4 — Target cohort is AI + DS, both genders. Missing data is operational.
> *"do not care about the data we have — any missing data will be completed when
> using the app for real; what we have completed now is the data for AI, DS M and F"*

`scheduler` is built and proven against **AI, AI2, DS, DS2 — M and F**. Measured:

| | M | F |
|---|---:|---:|
| students | 398 | 612 |
| lecture rooms | 8 (cap 25–60) | 5 (cap 30–55) |
| lab rooms | 2 (cap 25) | 4 (cap 24–40) |
| instructor mappings | 19 | **0** |

**Readiness is informational, not a gate.** Gaps (≈52% of requirement rows lack
`max_capacity`; the F cohort has zero instructor mappings) get filled by real use
of the app — they are not design problems to engineer around. The snapshot
*reports* them so the registrar knows what to complete, then proceeds. Only a gap
that makes solving literally impossible (no rooms of a required kind, zero demand)
stops a run.

Two facts worth carrying forward: the **F cohort is the harder instance** — more
students (612 vs 398) on *fewer* lecture rooms (5 vs 8) — and F has no instructor
data at all, so instructor quality on F must report `EVIDENCE_GAP` rather than a
flattering number.

---

### D5 — Instructor linkage is permanently partial. That is normal, not a gap.
> *"we have some instructors not linked but that is ok — we will not have all the
> instructors linked even when we start real working, so we should not expect all
> courses to have an instructor linked"*

A section **without** an instructor is a valid, complete section. Instructor
constraints (clash, daily cap, working days) apply only to the linked subset, and
that subset will never be 100%.

Three consequences, enforced throughout:

1. `Section.instructor_id` is `Optional` **by design** — not a nullable field
   awaiting backfill. Unassigned is a first-class state, never a degraded mode.
2. **No instructor metric may be published without its coverage.** "12 working
   days" over 15% of meetings is not a statement about the timetable. Every
   instructor figure carries `covered_meetings / total_meetings` beside it.
3. Missing linkage never blocks a build and is never reported as an error. It is
   reported as *scope*.

This supersedes the earlier plan to report `EVIDENCE_GAP` until instructor data
was complete — it will not become complete, so a design that waits for it is a
design that never runs. (Note the measured asymmetry: the M cohort has 19
mappings, the F cohort has **zero**. Both must build normally.)

Still relevant from the old framing: `course_instructors` is an *eligibility*
relation (program + course_code + gender), so it says who *may* teach a course,
not who *does* teach a section. Treating eligibility as assignment would
manufacture both clashes and apparent coverage — `scheduler` keeps them distinct.

### D6 — Timing family follows duration; room family follows kind
> *"for 100 minute lecture room we can use like the lab timing slots in the
> lecture rooms but will be used only for 100 min while insure no conflict with
> the other next lecture if it was in the same room"*

A meeting's **legal times** come from its *duration*; its **room family** comes
from its *kind*. They are independent. So a 100-minute lecture runs at the
100-minute windows (09:00–10:40, 10:45–12:25, 13:00–14:40, 14:45–16:25,
16:30–18:10) while occupying a **lecture** room.

The old engine inferred both from duration, which is precisely why one board
could be judged valid under one reading of the rule and invalid under another
(93/136 vs 42/136 passing, 0 under both).

**Load-bearing consequence:** a 100-minute meeting at 09:00–10:40 overlaps a
75-minute one at 10:30–11:45 by ten minutes. Room exclusivity must therefore be
tested by **interval overlap**, never by equal start times. Stated once in
`Grid.windows_for()`; every predicate derives from it.

**Corrected supply arithmetic.** Counting declared cells overstates room capacity:
10:30/10:50 and 14:30/14:45 are one opportunity offered two ways, not two. A room
can only hold pairwise non-overlapping meetings, so the 7 declared lecture cells
collapse to **5 real ones per day**. Real lecture-room capacity is
`rooms × 5 days × 5`, i.e. **125** for the 5-room female pool — not 175. Computed
by exact earliest-finish-time interval selection and verified against brute force.

### D7 — A room shortage never blocks. It reports unassigned rooms.
> *"if no enough rooms it shouldn't block the builder but will show the lectures
> as unassigned rooms"*

**Time is structural; a room is an assignment that can be left unmade.** A meeting
with no legal *window* cannot exist, so that blocks. Too few *rooms* does not: the
builder schedules the meetings in time and reports them as **unassigned room**.

This is superficially what the old engine did — it also left meetings unroomed.
The difference is honesty: it wrote a bare `UNASSIGNED` string with no count, no
cause, and no way to distinguish an unavoidable inventory shortage from a solver
that gave up. Here the shortage is stated before the build, with the arithmetic
that proves it, and the unroomed meetings are named after it.

Net effect on the target cohorts: **both are now `READY`.** The female cohort
still carries a genuine `WARNING` — 204 lecture meetings against 125 room-periods,
so at least **79 meetings will be scheduled with no room**. That is a real
resource fact for a registrar to act on, not a solver failure.

**Update (2026-07-27): the count was still not actionable, so it is now
decomposed.** "14 unroomed on the male cohort" could mean the university needs
another lab, or that this board stacked meetings into one hour while the rest of
the week sat empty — opposite fixes, indistinguishable from a total.
`scheduler.rooms.room_shortfall` sorts every unroomed meeting into:

* **IMPOSSIBLE** — no room of the right kind, open to that programme, is large
  enough. No timetable can place it, ever;
* **CONGESTION** — compatible rooms exist but were occupied at that hour
  (*verified against the board*, not inferred). Only this part is recoverable.

What that immediately found, and no aggregate would have:

| cohort | course | needs | largest room open to it | meetings |
|---|---|---|---|---|
| M | CS111 | lab seating 35 | **25** | 3 |
| M | CS113 | lab seating 27 | **25** | 3 |
| M | CS372 | lab seating 27 | **25** | 2 |
| M | CS424 | lab seating 26 | **25** | 2 |
| F | FE1 | lecture seating 60 | **55** | 3 |
| F | FE2 | lecture seating 60 | **55** | 1 |

Ten of the male cohort's fourteen unroomed meetings are **four CS courses whose
labs are one to ten seats too big for either lab room** — and the estate is only
53% utilised, so no amount of solver effort touches them. The fix is a larger lab
or a smaller section, which is a decision for a human, stated as such.

### D8 — No room turnover. Tight transitions are deliberate.
> *"all these are acceptable for better room utilisation and reducing student
> gaps time"*

H9 (room exclusivity) is **raw half-open interval overlap of teaching time**.
There is no turnover/changeover allowance, so two meetings may sit back-to-back
in the same room, and touching windows (16:00 immediately after a 14:45–16:00
lecture) do **not** conflict.

This deliberately rejects the external spec's `[start, teaching_end + turnover)`
effective-occupancy model (lecture 15 min / lab 5 min). Measured, that model
would cost **nothing in room capacity** — the bound stays 5 meetings per room-day
either way, because the 75-minute lecture grid already has 15-minute gaps built
in (09:00 → 10:30 is exactly 75 + 15). What it would cost is **flexibility**, and
precisely the transitions that shorten student days:

| transition | permitted here | under a 15-min turnover rule |
|---|---|---|
| 100-min lecture ends 10:40 → 10:50 lecture | yes (10-min gap) | blocked — student waits to 13:00, a **140-min** gap |
| 14:45–16:00 → 16:00–17:15 | yes (touching) | blocked |
| consecutive 100-min lectures at lab timings | yes (5-min gap) | blocked |

So turnover would forbid six same-room pairs, buy no extra capacity, and lengthen
student days. The owner's judgement is that a 0–10 minute changeover is
acceptable in exchange for utilisation and shorter gaps.

**Consequence for D6:** the 100-minute-lecture rule is safe as written. The
earlier concern that it needed turnover protection does not apply, because
turnover is not a rule here.

### D10 — Instructor load: ≤2 sections of one course, and the rest go unlinked
> *"if an instructor is linked to a course and the course has more than 3
> sections, the max allowed per instructor for the same course is 2 sections and
> the rest will be not linked"*

An instructor may hold at most **2 sections of the same course** once that course
runs more than 3 sections. Sections beyond that are simply **left unassigned** —
consistent with D5, where unlinked is a first-class state rather than a gap.

**Measured: this rule does not bind on today's data.** No course any instructor is
eligible for runs more than 3 sections — all have 1 or 2. It is recorded as policy
for when section counts grow, not as a fix for the current load problem.

**The load problem is breadth, not duplication.** Instructor 21 is eligible for
*six different courses* — 9 sections, 20 sessions, a floor of **7 working days in
a 5-day week**. Capping sections-per-course cannot help, because no course
contributes more than 2 to begin with.

**Therefore: the weekly cap is implied by the daily cap, and needs no new data.**
H8 already limits an instructor to 3 sessions per day, and the week has 5 teaching
days, so:

```
max sessions per instructor per week = daily_cap x teaching_days = 3 x 5 = 15
```

Instructor 21's 20 eligible sessions must therefore shed at least 5 — roughly two
sections — which go **unlinked**, exactly as D10 prescribes for the per-course
case. No policy invention is required; the limit falls out of a rule that already
exists.

(`Instructor.max_weekly_hours` exists on the model but is `None` for all ten
instructors. If real per-instructor limits are ever entered, they override this
derived default — until then the derived one is used and reported as derived.)

### D11 — Working days rank above gaps, and gaps are bought as hard as that allows
> *"what is important for me is to build the instructor timetable with the
> minimum gap possible while respecting all other constraints"*

Days and gaps **genuinely oppose each other**, which was not obvious until it was
measured: spreading an instructor's sessions across more days shortens each day
and cuts idle time, while packing them into fewer days lengthens each day and
creates gaps. Both cannot be minimised at once.

Measured on the live M cohort (45s, alpha 0.9), varying the per-minute idle cost:

| idle cost/min | expected clashes | working days | idle minutes |
|---|---|---|---|
| 0 | 90.5 | **19 = floor** | 2495 |
| 0.05 | 99.9 | **19** | 1615 |
| 0.2 | 96.1 | **19** | 1430 |
| 0.3 | 102.5 | **19** | 950 |
| 0.4 | 94.1 | 20 | 1035 |
| 1 | 98.2 | 20 | 730 |
| 3 | 97.2 | 25 | 420 |

> **These figures are kept for the shape of the trade-off only — do not quote the
> absolute numbers.** They were taken before two model defects were found: a
> weight of `0` could not switch the gap term off (it was floored to 1 point per
> idle minute, so even the "0" row was quietly optimising gaps), and the sibling
> symmetry break was ordering sections across *different instructors*, deleting
> genuinely distinct timetables. With both fixed, the same gap-off reference
> measures 3150–3260 idle minutes rather than 2495, and is far more stable
> (19/19/19 working days, 92.8–95.4 clashes across seeds).

**A weight cannot promise anything about a quantity it merely trades against.**
The knee above (0.3) looked like the answer, but re-running it across three seeds
gave **19, 20 and 20** working days. There is no setting that reliably holds the
floor, because the solver is free to spend a day whenever the idle saving happens
to outweigh it on that particular search path.

**Decision: settle the days first, then freeze them as a budget.**

1. pass 1 minimises working days, with student clashes weighted as usual;
2. pass 2 re-solves with `working days <= whatever pass 1 achieved` as a **hard
   constraint**, and only then lets the gap term push as hard as it likes.

Pass 2 cannot fail and cannot regress: pass 1's own board satisfies the budget,
so it is always available as a fallback, and it is passed in as a solution hint.
With days protected by a constraint rather than a weight, the gap weight no
longer has to be conservative — it is set an order of magnitude above the knee,
because there is nothing left for it to spend.

**And then it spent the students instead.** With only the day budget in place,
one seed reached 620 idle minutes while expected clashes rose to 132 against
roughly 104. Freeing a term from one constraint simply moved the cost somewhere
unwatched. So pass 2 carries a **second** ceiling: clashes may exceed pass 1's by
at most `clash_tolerance`. Gaps are minimised inside both budgets, and what
students give up is a declared number rather than a discovered one.

The ceiling is expressed in the solver's own integer units, not in the
recomputed float — the two differ wherever a weight was rounded, and a
zero-tolerance ceiling derived from the float can reject the very board that
produced it.

**Measured clean, after the two model defects above were fixed** (M cohort, 120s,
three seeds each, quiet machine):

| | working days | idle minutes | expected clashes |
|---|---|---|---|
| single pass, gap term off | 19 / 19 / 19 | ~3255 | 94.1 |
| **`plan()`, 5% tolerance (default)** | **19 / 19 / 19 = floor** | **1635** | **88.3** |
| `plan()`, 20% tolerance | 19 / 19 / 19 = floor | 645 | 100.7 |

At the default the planner **dominates** the previous behaviour: the same proven
day floor, half the idle time, *and* fewer student clashes. That is not a
trade-off being struck well — it is the removal of two defects that were costing
both constituencies at once. Buying gaps at the students' expense only begins
above the default, which is what `--clash-tolerance` is for.

Every instructor sits on their own proven minimum number of working days in all
nine runs — the day count is no longer a matter of luck.

This is the epsilon-constraint method, and it is the right shape for this problem
precisely because the two quantities are **not** commensurable: a commute and an
hour of waiting are different kinds of cost, and pretending an exchange rate
exists between them was what made the result unstable.

`--span-weight` still moves the second pass for anyone who wants to trade gaps
against student clashes; it can no longer cost anyone a working day.

Minimising span is *exactly* minimising idle, not an approximation: summed over
instructor-days, `span = teaching + idle`, and total teaching is constant once
every session is placed somewhere.

**This began as a defect, not a tuning exercise.** The idle term was scaled by
`(1 - alpha)` and cast with `int()`, so `int(2 * 0.1)` was `0` and the surviving
`max(1, ...)` floor priced a minute of an instructor's time at 1 point against
10,000 for a working day. The term existed, was documented, and did nothing. Idle
was 2250 minutes against a 400-minute lower bound. Weights now live in one
currency (1000 points = one expected clash) and are only ever scaled on the
instructor side.

---

## 7. Still open

1. **The default-capacity value** (see D3) — S1 takes it as an explicit flag and
   reports its blast radius.
2. **Who assigns instructors to sections** — a human decision surfaced by the UI,
   or a solver objective over the eligibility relation? Deferred to S3; S1 only
   needs to carry eligibility and report coverage.
3. **Does a shared course need a room serving *all* its programmes?** Every live
   call site tests overlap (`offering.programs & room.programs`), so a class
   taken by AI and DS students may sit in an AI-only room. A dead helper in
   `entities.py` asserted the stricter subset rule; it was removed rather than
   applied, because switching would shrink the usable estate sharply — `AIR1`
   and `AIR2` serve only `AI`, while most offerings carry all four programmes —
   and that is a policy call, not a tidy-up. **Owner decision needed.**

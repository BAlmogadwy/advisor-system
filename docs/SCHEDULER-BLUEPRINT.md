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
| N1 | **Immutable identity.** `OfferingId`, `SectionId`, `MeetingId` are opaque and stable. Display `course_code` is never an identifier. | `FE1` and `CS111` are each *two* offerings with different demand; joining on the display code silently merges them and corrupted the overlap matrix (250 code-level pairs vs 253 real). **This subsystem broke its own rule and had to be fixed on live data (2026-07-28):** offerings were grouped by bare `course_code`, so `CS111` "Fundamentals of Programming" (AI2/DS2, plan term 1) and `CS111` "Programming I" (AI/DS, plan term 3) became one offering with pooled demand — the AI and AI2 plans are offset by one term, so what AI calls CS111, AI2 calls CS112. Identity is now `planner_course_key`, the same key `compute_section_plan` and `ScenarioSectionBudget` already use. |
| N2 | **Sections are board-independent.** A section has one schedule; board/term membership is an explicit many-to-many, never a duplicated placement. | 22 of 48 shared sections are currently scheduled at *two different times at once* — physically impossible. |
| N3 | **Snapshot-based solving.** Input is extracted once into an immutable, fingerprinted `Snapshot`. The solver sees only that — no Django, no DB. | Today the 7-strategy generate performs ~8 full scenario builds *against live production tables*, using the DB as scratch space. |
| N4 | **Rooms and instructors are decision variables**, in the model from the first solve. | The current optimiser is room-blind by construction (`build_room_state_for_scenario` has zero callers; `rooms_by_id=None` at all four search sites; no room term in the objective) — so it *creates* boards that cannot be roomed. |
| N5 | **Compiled meeting requirements.** An offering declares its exact weekly multiset (kind, delivery mode, duration, count); credit hours are a default, not truth. | Meeting shape is currently guessed from credit hours + duration heuristics that disagree across surfaces (three different lab-duration literals). |
| N6 | **One rulebook.** Each constraint is defined once, with adapters for check / delta / native-CP-SAT. | The same rule had up to 8 divergent implementations; that drift is what the current engine spent two PR cycles undoing. |
| N7 | **Honest multi-objective.** Lexicographic only over *hard-feasibility* tiers; quality is an explicit weighted trade with published weights, and the engine can emit **alternatives** rather than one take-it-or-leave-it board. | The current 9-tuple collapses in practice — measured `(0,0,0,0,87060,56,32,52465,0)`: positions 0–3 and 8 never differentiate, so search is driven by one weighted sum. |
| N8 | **Provenance, and an honest account of variance.** Every run stamped with input/rules/config/code/seed fingerprints; runs with different fingerprints are not comparable. Byte-identical reproducibility is **NOT** claimed — see the correction below. | Optimiser runs are routinely compared across code changes with no record of what produced them. |
| N9 | **Independent validation.** The checker shares the *specification* with the solver but none of its fast occupancy/delta code. | A solver that grades its own homework cannot detect its own modelling errors. |
| N10 | **Certificates, not verdicts.** "Infeasible" always carries the reason and a number — which resource, which bound, which witness. | Today a blocked board says "blocked"; the registrar cannot tell an unavoidable inventory shortage from a solver that gave up. Already proven valuable: the exact-rooming pass showed 8 meetings unroomable at fixed times, and a female cohort short **53 lecture-periods** — facts no solver can fix. |
| N11 | **Incremental evaluation.** Re-score only what a move changed. | One candidate evaluation currently costs **22 ms** (deepcopy + re-seat all 390 students + full rescore + a second quality pass); at ~10⁴ evaluations that is 3.7 minutes of pure scoring per click. |
| N12 | **Bounded everything.** Every search stage has a mandatory deadline; no unbounded default. | `TIMETABLE_CHAIN_TIME_LIMIT_SECONDS` still defaults to `0` = unbounded — the setting behind an 8.7-hour runaway job. |


### N8 corrected (2026-07-27): reproducibility was promised and cannot be delivered

The original wording was *"same snapshot + same config + same seed ⇒ byte-identical
board"*. **It does not hold, and buying it costs more than it is worth.**

Measured: three runs of an identical pass 1, same seed, gave expected clashes
**95.8 / 86.3 / 106.4** and three different objective values. The cause is
structural — eight search workers racing a **wall-clock** deadline stop wherever
the machine happened to be, so the incumbent depends on load rather than on the
model.

Both reproducible configurations were tested, and both are far worse:

| configuration | reproducible | expected clashes |
|---|---|---|
| 8 workers, wall clock (shipped) | no | **96.7** |
| 1 worker, wall clock | yes | 391.0 |
| 8 workers, deterministic time | no | 104.4 |
| 8 workers, interleaved + deterministic time | yes | 313.4 |

**Decision: keep the quality, drop the promise, and manage the variance
explicitly.** A timetable three to four times worse, delivered identically every
time, serves nobody.

What replaces it:

* `plan_portfolio` runs several independent attempts and keeps the best, and
  **records the spread it chose from** in the run notes, so a lucky board is
  distinguishable from a reliable one;
* **no decision is ever taken from a single run.** Every tuning figure in this
  document is a median over seeds with its range shown. One sweep that ignored
  this produced a "weight 10" row showing *worse* back-to-back pairing than the
  feature switched off, and a lost working day — pure noise, read at the time as
  a real effect.

Provenance is unaffected and still required: a run without its fingerprints is
not comparable to anything.

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

### D9 — Online teaching: no room, no commute, one window of its own

Online courses (GS/GSE, 2 credits, one 100-minute session a week) consume **no
room** and create **no campus travel**. That much has never changed.

What did change, on 2026-07-28, is where they run and whether they can clash.

**Originally:** a private late-day family — 15:00, 16:45, 18:30 — placed after
the on-campus day so it competed with nothing, which is what licensed the second
half of the rule: *online never clashes for a student*.

**The problem that forced a revision.** That family was the only thing this
engine scheduled at times the workspace scenario's own grid does not declare. To
draw such a class the seam has to widen `lab_slot_config` — and that field is not
a display list, it is the **legal placement set the existing engine reads**
(`timetable_autoplace._generate_meeting_options`,
`timetable_workspace` and the CP-SAT polisher all build their moves from it).
Measured: a scheduler build on an AI/M scenario added `18:30-20:10` to the lab
columns, after which running "Optimise Current" could put a **room-consuming lab
at 18:30**, a time nothing in the estate is open. That is the only place in the
subsystem that could regress the engine it was built beside, and that constraint
is stated as absolute.

**Decision.**

1. **Online runs at the declared hours** — the same 100-minute family as
   everything else of that length. Nothing has to be widened, ever.
2. **The clash exemption goes with it.** A class at 13:00 occupies a student's
   13:00 whether they attend it in a room or at home, so online now clashes like
   anything else and counts toward an instructor's daily cap.
3. **It is still not campus presence.** Working days and campus idle exclude it:
   teaching a session from home does not turn a free day into a commute, and it
   cannot fill a gap between two on-campus classes. (Both the model and
   `instructor_metrics` had to be corrected for this — they were counting online
   as time spent at the university, which would have had the gap objective
   dragging online sessions around to close gaps that do not exist.)
4. **Online has three windows of its own and no others** — `15:50-17:30`,
   `17:40-19:20`, `19:30-21:10` — and **no other course may use them.** The
   exclusivity runs both ways and both directions are enforced: the grid gives
   online meetings only these three (families are keyed on duration *and*
   delivery), and the same three are flagged `online_only` in the shared slot
   config, which every automatic placer filters through `placeable_slots()` —
   the option generator, the CP-SAT polisher and the lab availability grid
   alike. **Manual** placement is deliberately unaffected, because a human
   putting an online course in one is making a decision.

   They are declared in the shared config rather than invented by the new
   engine, so a scenario grid never has to be widened to draw them.

5. **Online is not student waiting time** (owner rule). It still CLASHES — an
   online class at 15:50 runs straight through a 16:00 lecture and a student
   cannot attend both — but the hours between an afternoon lecture and an
   evening online session are not a student hanging about between classes, and
   counting them would swamp the figure. Clash over every meeting; waiting over
   in-person meetings only. The instructor side already worked this way.

**What (1) and (2) cost** — M cohort, 74 sections, same section set both arms,
120 s, seeds 0/7/21, students actually seated:

| | working days | seated clash-free | expected clashes *(proxy)* |
|---|---|---|---|
| private late family | 19 / 19 / 19 = floor | 99.5 / 97.7 / 99.7 % | 75 / 77 / 90 |
| declared hours | 19 / 19 / 19 = floor | 99.5 / 99.2 / 99.5 % | 99 / 116 / 130 |

The proxy rises about 50%, and **the outcome does not**: the seated median is
identical, and the new arm's *worst* seed is better than the old arm's worst. The
proxy assumes students land in sections at random and so counts collisions that
seating routes around — the same pattern D14 records. Item (4) then hands the
clash term somewhere to put an online class that would otherwise sit on top of a
lecture, and the day count returned to the floor on the end-to-end run.

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

### D12 — Instructors are linked to the department's OWN courses only
> *"in reality we set only the instructors for the courses of the same programme
> like courses of AI or DS only; for other courses what we try to keep only
> sections back to back if they were of the same course"*

Staffing data exists for `AI*` and `DS*` courses. Everything else — `CS`, `MATH`,
`GS`, `ENG`, `PHYS`, `STAT`, `COE`, `ENGL`, `CHEM`, `GSE`, `FE` — is a service
course run by another department, and this system will never learn who teaches
it.

**Measured on the male cohort: own courses are 24 of 88 sections (27%), and 23
are already assigned.** Coverage is therefore at **96% of everything staffable**.

**This retracts a claim made repeatedly in earlier notes** — that "the biggest
remaining lever is instructor coverage at 26%". It is not a lever; it is a
ceiling, and the data is essentially complete against it. The synthetic sweep
that pushed coverage to 66% was measuring a department that will not exist.

**For the other 73%, the rule is different: keep the sections of one course back
to back.** Whoever teaches them should be called in once rather than three
times — the same care the instructor objective gives named staff, extended to
staff the data cannot name.

It is a **matching, not a chain**: a section pairs with at most one sibling, so
three sections give one pair and a leftover — exactly as described. Rewarding
every adjacent combination instead would pay twice for a three-section block and
drag four sections into one long run, which is not what was asked and costs the
student objective far more.

It is a **reward**, not a constraint, because it opposes the student objective
directly: that one wins by *spreading* sibling sections (a colliding pair costs
`shared / (na x nb)`), and this pulls them together.

**Measured over three seeds per weight, medians with ranges** — single runs of
this solver are not comparable (see the N8 correction):

| weight | service-course pairs back to back | expected clashes | working days |
|---|---|---|---|
| 0 (off) | **15%** [9-19] | 101.6 [93-104] | 19 = floor |
| 1 | 54% [54-67] | 106.0 [99-113] | 19 = floor |
| **3 (default)** | **70%** [65-80] | **101.5** [99-109] | 19 = floor |
| 10 | 83% [70-93] | 115.6 [96-117] | 19 = floor |

**Default 3, and on.** The pairing gain at that weight is unambiguous — the
ranges do not overlap — while the clash medians are indistinguishable from
having it switched off. An earlier single-run sweep put the cost at +23 clashes;
that was noise, and re-measuring properly removed it.

**The reward runs in the second pass only.** Letting it act in pass 1 — whose
sole job is to settle how few days instructors work — pushed the day count off
its proven floor from weight 3 upward (19 to 20 to 21). Worse, pass 2's day
budget is derived from pass 1, so the loss was locked in and unrecoverable.
Preferences do not belong in the pass that establishes a guarantee.

"Back to back" means the next teaching slot, not literally touching: this grid
runs 09:00-10:15 then 10:30, so consecutive slots carry a 15-minute changeover
while the next real gap is 55 minutes. The threshold sits between them.

Scope: 29 courses run more than one section (75 sections), and **20 of those are
service courses** (57 sections) — `ENG101` x4, `GS101` x4, `MATH105` x4,
`PHYS103` x4, `CS111` x3.

### D13 — Students and instructors compete for the same lever

Seating real students (the confirmation N3 always specified and which had never
been run) showed the board is essentially clash-free — 1 student of 390 on the
male cohort, 0 of 612 on the female — but that every student loses roughly ten
hours a week to gaps between classes, and **nothing was optimising it**.

A term was built for it: co-demanded courses are rewarded for being *adjacent*
rather than merely non-overlapping, on the same `shared / (na x nb)` currency the
clash term already uses. Measured by seating, two seeds, medians:

| weight | student waiting | instructor idle | clash-free | working days |
|---|---|---|---|---|
| **0 (default)** | 573 min | **1262** | 100% | 19 = floor |
| 1 | **465 min** (-19%) | 1532 (+21%) | 100% | 19 = floor |
| 3 | 498 min | 2100 (+66%) | 100% | 19 = floor |
| 10 | 474 min | 2078 | 100% | 19 = floor |

**Packing a student's day spreads an instructor's, and the reverse.** They are
the same lever pulled from opposite ends. Buying students 19% of their waiting
at a 21% cost to instructors is not a trade this system should make silently
when the owner's stated priority is *"the instructor timetable with the minimum
gap possible"*.

**Decision: off by default, offered as `--student-gaps`.** Weight 1 is the only
setting worth using — 3 and 10 are worse for **both** parties, which is a useful
reminder that pushing harder on a soft term is not the same as doing better.

Two seeds is a thin sample and the differences between 465, 474 and 498 are
inside the noise; the instructor cost is the part that looks consistent.

### D14 — A section keeps the same hour all week

> *"for a section, let's say the first lecture was 9am — the next lecture for
> that section is not good to be after noon, or late like after 15:00.
> Preferably keep the section at the same time slots if possible; if not, one
> slot before or after, not too far."*

**Nothing in the model had an opinion about this.** Every other term treats a
section's two weekly meetings as unrelated events: the clash term only cares what
sits on top of a meeting, and the instructor terms only care which *days* are
used.

The baseline says exactly that, and says it precisely. On the seven-start lecture
family, meetings placed **independently** would land on the same hour 14.3% of the
time and within one slot 38.8%. Measured with no rule: **16.4–18.0%** and
**34.4–37.7%**. The model was *indifferent* — not, as an earlier draft of this
section claimed, actively scattering. The worst section wandered **420 minutes**,
seven hours, in all three runs.

That distinction matters for reading the cost below: the ceiling costs clashes
because it removes placement freedom in general, not because far-apart meetings
specifically collide less.

#### The ceiling is counted in slots, and bounded again in minutes

The 75-minute lecture family declares

    09:00   10:30   10:50   13:00   14:30   14:45   16:00

whose rank-adjacent steps are 90, 20, **130**, 90, 15 and 75 minutes. No single
minute threshold expresses "one slot":

* at the smallest step (15) it forbids 09:00 → 10:30, which the rule permits;
* at the largest (130) it permits **10:50 → 13:00**, which crosses noon and is the
  move the rule exists to stop.

So `max_time_of_day_slots` counts **rank** — how many declared starts apart the
meetings are. But rank alone leaves precisely that one bad pair legal on this
grid, and it was the worst case in **all nine** measured runs, at exactly 130
minutes. `max_time_of_day_minutes` bounds the real gap alongside it. Each unit
says something the other cannot.

*(An earlier version of this argument used 09:00 → 13:00 as the example of what a
130-minute ceiling permits. That is 240 minutes and a 130-minute ceiling forbids
it; the example was simply wrong, and the correct one is 10:50 → 13:00.)*

Compared **within a timing family**, which the grid defines by duration and
delivery (D6: timing follows duration, room follows kind). A 75-minute lecture and
a 100-minute lab come from different declared families with different start times,
and demanding they line up would be a rule the grid cannot satisfy. Two meetings
of equal duration share a family even if one is declared a lab and the other a
lecture — that is D6's rule, not an oversight.

#### A ceiling, not a weight

A ceiling is a guarantee; a weight is a preference the search can outbid, and this
search is already competing against a hard clash budget inside a 45-second
half-pass. A priced variant existed briefly and was **deleted**: it was never
exposed by any caller, no test could distinguish it from being absent, and the
minute ceiling expresses "not too far" as a guarantee rather than a hope.

The ceiling applies in **both** passes of `plan()`. Pass 2's clash budget is
derived from pass 1's score, so a ceiling applied only in pass 2 would be measured
against a total achieved without it and would be infeasible on arrival — silently
falling back to a pass-1 board that ignores the rule entirely.

#### What it costs

> **STALE — being re-measured.** The table below was taken before the priced
> variant was deleted, and deleting it removed ~250 integer variables and
> equalities from **pass 1**. A control re-run of the 1-slot arm on the current
> code, same seeds, produced materially different instructor-idle figures
> (1145–3895 against 1200–1370). An optimisation of my own therefore invalidated
> my own measurement, and the numbers here describe code that no longer exists.
> Do not quote them.

M cohort (male, AI/AI2/DS/DS2), 1448 term 1, `--default-capacity 25`, 120 s,
**seeds 0/7/21, quiet machine**, median [min–max], students actually seated.

| | rule off | **1 slot (default)** | 0 slots |
|---|---|---|---|
| within one slot of itself | 37.7 [34.4–37.7] % | **100 %** | **100 %** |
| on the exact same hour | 18.0 [16.4–18.0] % | 36.1 [34.4–41.0] % | **100 %** |
| average wander | 174 [143–182] min | 55 [43–56] min | **0** |
| worst wander | 420 min ×3 | 130 min ×3 | **0** |
| students clash-free, seated | 99.7 [99.7–100] % | 99.2 [98.2–99.7] % | **100 % ×3** |
| — students affected of 390 | 1 [0–1] | 3 [1–7] | **0 ×3** |
| student waiting, per student | 481 [463–495] min | 476 [444–514] min | 515 [498–523] min |
| instructor working days | 18 ×3 | 18 ×3 | 18 ×3 |
| instructor idle | 2195 [1670–2405] min | **1220 [1200–1370] min** | 2095 [1375–2505] min |
| sibling sections back to back | 6.3 [4.8–33.3] % | 44.4 [31.7–44.4] % | 9.5 [7.9–50.8] % |
| unroomed | 16 [14–18] | 18 [18–19] | 17 [15–20] |
| expected clashes *(proxy)* | 60.2 [53.3–60.2] | 86.3 [72.8–92.6] | 82.5 [80.8–93.0] |
| hard violations | 0 ×3 | 0 ×3 | 0 ×3 |

Reading it honestly, claim by claim:

* **The rule is delivered.** 100% within one slot is structural, not statistical —
  it is a hard constraint, and the three runs merely confirm the model is feasible.
* **Instructor idle improves, and this is the one result with clean separation.**
  1200–1370 against 1670–2405: the ranges do not overlap. Not quoted as a
  percentage, because the seed-to-seed ratio admits anything from 18% to 50%.
* **Students pay a little, and every order statistic moved the wrong way.** Not
  "unchanged": clash-free went 99.7/99.7/100 → 98.2/99.2/99.7, which is 1, 3 and 7
  students of 390 rather than 0, 1 and 1. Small, real, and worth stating as a
  distribution rather than as a median. Note too that clash-free is itself the
  output of a 120-second CP-SAT seating solve, so it carries its own noise.
* **Rooms get slightly worse and this is the least comfortable number here.**
  18 [18–19] against 16 [14–18] — worse at every rank. `choose_run` ranks rooms
  above everything but working days, precisely because a class with nowhere to meet
  cannot be taught at all, so this is not a rounding error even at two meetings.
* **Student waiting is unchanged.** 476 against 481 is a 1% median gap with almost
  fully overlapping ranges and two of three seeds worse. It is not a gain.
* **Sibling back-to-back is suggestive, not established.** 44.4 [31.7–44.4]
  against 6.3 [4.8–33.3]: the medians are far apart but the ranges **overlap** —
  one seed with the rule off reached 33.3%, above the worst seed with it on. Three
  seeds cannot settle this.
* **The proxy is not the outcome.** Expected clashes rise about 43%, and the real
  seated cost is the two-to-six students above. The proxy assumes students land in
  sections at random; seating dodges collisions it assumes unavoidable.
* **No mechanism is claimed.** An earlier draft said the gains came from the week
  becoming "more regular". The 0-slot column refutes that: it pins every section
  to exactly one hour — maximum regularity — and returns idle 2095 and back-to-back
  9.5%, both indistinguishable from having no rule at all. The response is
  **non-monotone** in the tightening, and that is a reason to treat the 1-slot
  result as unexplained rather than understood. It may yet be a search artefact.
* **Nothing here says a tighter ceiling searches better.** The proxy at 0 slots
  (80.8/82.5/93.0) and at 1 slot (72.8/86.3/92.6) are indistinguishable, and 0 is
  worse in two of the three seeds; only the medians reverse. An earlier draft read
  a story into that. It is noise.

#### Decision

**`--same-time-slots 1` with `--same-time-minutes 100`, on by default.** One slot
either side is the rule as stated; the minute bound closes the one gap rank cannot
see, the 10:50 → 13:00 step that is one slot wide and still crosses noon. `0`
demands the identical hour every day; a negative number switches either off.

`0` is the best setting for **clash-free seating** — 100% on all three seeds — and
the **worst** of the three for student waiting (515 min, worse than no rule at
all). It is offered, not defaulted, because the owner's stated priority is the
instructor timetable and that is where the 1-slot setting is unambiguously ahead.

Unlike the other hard budgets in `plan()`, this one is the caller's policy rather
than something derived from a board already in hand, so it can genuinely have no
solution. A cohort that cannot meet it still gets a timetable, and the compromise
goes into `SolveResult.warnings` — a channel separate from `notes`, because every
successful two-pass run writes a note, and a screen that renders both as warnings
teaches the reader to ignore warnings. `plan_portfolio` ranks on the rule too,
between rooms and waiting, against **the ceiling that was actually asked for**
rather than a hardcoded one, so a seed that kept the rule is never beaten by one
that abandoned it.

#### What this measurement does not cover

* **One cohort.** Male, AI/AI2/DS/DS2, one term, one machine, three seeds. The
  **female cohort was not measured at all** — and F has no `course_instructors`
  rows, so the headline instructor-idle result is not even defined there.
* **"18 = floor" is observed here, not proven in this experiment** — 18 on all
  nine runs. It is also **not comparable with the 19 in D11–D13**: the section plan
  changed when sectioning moved to the project's own planner, so those sections'
  absolute numbers should not be read against these.
* **Row "student waiting" is minutes per student per week**, an average, not a
  total.

### D15 — A collision is priced by whether the student can go elsewhere

> *"I introduced the tier system because these courses are available in other
> sections under our college, all departments — so I am sure they can find seats
> for them in those sections. I prefer not to sacrifice the idle time for those
> I am sure I can register elsewhere."*

The project already classifies every course (`core.services.timetable_course_tier`):
**T1** specialised major, **T2** shared foundation (MATH/STAT, or required by
more than two plans), **T3** general education and free electives (ENGL, GS,
GSE, FE). Until now the timetable ignored it and priced every collision the same.

Measured on the male cohort, the clash objective divides like this:

| pairs | share of the objective |
|---|---|
| involving a **T3** course | **46.6 %** |
| involving T2 (not T3) | 37.9 % |
| **T1 against T1** | **15.5 %** |

**Nearly half the engine's effort was spent defending collisions the registrar
can resolve by other means.** A pair now takes the **lower** of the two tiers'
weights — if either course can be picked up elsewhere, the clash is resolvable,
so the pair is only as serious as its most relocatable member.

**What it costs — M cohort, 3 seeds, students actually seated:**

| | T1↔T1 collisions | instructor idle | seated clash-free |
|---|---|---|---|
| undiscounted | 1.8 – **16.0** | 1485 – 1805 | 98.5 – 99.5 % |
| **T1 1.0 / T2 0.5 / T3 0.2** | **3.0 – 3.8** | 985 – 2305 | 99.0 – 99.5 % |

The medians barely move. What changes is the **spread**: undiscounted, the number
of collisions in the courses that matter swings between 1.8 and 16.0 on nothing
but the random seed. Discounted, it is 3.0 to 3.8 every time. *A number nobody
can predict is worse than a slightly higher one they can*, and that is the whole
argument for the default.

The classifier is **imported, not restated**, through one named exemption to the
isolation rule (`POLICY_MODULES` in the boundary test). That rule exists to keep
the old **builder** out; `timetable_course_tier` is a pure classifier over
`ProgrammeRequirement`. Every expensive mistake in this subsystem has come from
restating policy instead of consuming it — section sizing, elective resolution,
the cross-term split. One exemption is cheaper than a fourth divergent copy.

---

### D16 — Waiting time is optimised for the students whose week can be tidy

> *"It would be better to calculate the student idle time for the full regular
> timetable — students who take full-term courses, not mixed terms. For the
> others we just guarantee they can register with no clashes."*

A student taking a coherent term-N block has a week that **can** be made compact.
One picking up leftovers from terms 3, 5 and 7 has a scattered set by
construction, and pulling their courses together drags the whole board for a
tidiness that is not achievable.

Using the project's own rule — a student is cross-term when their recommended
courses span more than one curriculum term, mapped through **their own
programme's** plan — the male cohort splits **238 regular / 152 cross-term**.
(The per-programme scoping is load-bearing: IS has FE1 at term 7 and IS2 has it
at term 8, so a merged map files half a cohort on the wrong board.)

Waiting is now **reported separately** for the two groups — averaging them hid
both — and the waiting objective counts regular students only.

**Clashes are still counted for everybody.** The guarantee that a student can
register at all is not restricted to anyone.

**Default OFF, and that is a measurement rather than caution.** Restricting the
objective to 61% of students did make it cheaper, but not free: regular waiting
falls about 13% (538 → 470 median) while instructor idle rises from ~1300 to
~2450. Students and instructors still pull the same lever from opposite ends
(D13), and the owner's stated priority is the instructor timetable. Offered as
`--student-gaps`; weight 1 is the only setting worth using.

`DUPLICATION, DECLARED`: the classification is reimplemented in `intake.py`
because the canonical version is inline in
`generate_workspace_scenario`, which cannot be called without also creating a
scenario, and the materialised answer lives on scenario rows this subsystem may
not read. Both ends carry the note.

---

### D17 — Non-T1 sections own a fixed block of slots

> *"For the other courses it is mandatory to have the sections on the same time
> slots only. If we have 2 sections for CS111, S1 and S2, both must be back to
> back — but we can alternate between them, so one day starts S1 then S2, another
> day S2 then S1. For the instructor they both occupy the same slots, because
> those instructors are from other departments and have other courses."*

D14's ceiling is **per section**, and that is right for this department's own
courses (T1), where we control the instructor. Everything else is taught by
people who also teach elsewhere in the week, and a course whose slots move about
is unmanageable for them.

* **one section** → keeps ONE slot, every day it runs;
* **a back-to-back pair** → owns an exact PAIR of slots, the same two every day,
  and the two sections may **swap** which of them they sit in:

```
Monday      13:00  CS111 S1     14:30  CS111 S2
Wednesday   13:00  CS111 S2     14:30  CS111 S1
```

The other department sees 13:00 and 14:30 occupied every week without exception.
Which section is in which is ours to decide, and the swap costs nothing while
giving the search somewhere to move.

**Scoped to the PAIR, not the course.** Scoping it to the course would force
every section of a four-section course onto the same days — far heavier than what
was asked. A third section stands alone with its own single slot and is not tied
to the pair's days. Pairs are formed by section order (S1+S2, S3+S4, leftover
last) so the other department can rely on it and the answer does not move between
runs.

**Online is exempt** (owner). GS and GSE already run in their own evening windows
(D9), consume no room and create no campus travel, so pinning them buys nobody
anything and spends the freedom in the one family that has slack.

**What it costs — M cohort, 3 seeds:**

| | groups keeping an identical daily block | T1↔T1 collisions | instructor idle | days |
|---|---|---|---|---|
| off | 16–19 of 38 | 2.5 – 3.2 | 1450 – 2615 | 19 = floor |
| **on** | **38 of 38** | 2.8 – 7.2 | 1160 – 3195 | 19 = floor |

Feasible on every seed, never dropped, zero violations. The cost is T1
collisions — median 3.0 → 3.8 — and everything else overlaps.

*Note the per-section "within one slot" figure falls to ~94% under this rule, and
that is correct rather than a regression: a pair's two slots need not be
adjacent, so a section legitimately moves further than one slot when it swaps.
The block rule supersedes the per-section ceiling for these courses; applying
both would forbid the alternation the rule exists to allow.*

---

### D18 — A course too few students want does not get a place on the board

> *"When we get the demand, any course with demand less than 5 students, drop it
> from the demand before running the planner. This also will reduce the search
> space complexity."*

A course three people want costs exactly what a course of forty costs: a room
for every meeting, a day opened on an instructor's week, a slot every other
course then has to avoid, and — because sections of one course may never overlap
(H10) — one of the week's **25 mutually non-overlapping cells**. The registrar's
answer for those three students is the same as the tier argument in D15: seat
them in a section that already runs somewhere in the college.

**Where it is applied is the whole design.** Between demand and section planning,
because that is the only point at which the rule means what was asked. Later, the
sections already exist and their cost is already paid; earlier, there is no
demand to count. It is deliberately **not a cascade** — dropping course X cannot
change how many students want course Y, so one pass is exact and no course can be
dragged under the floor by another's removal.

**What it removes, male AI/AI2/DS/DS2 1448 T1:**

| course | students | tier |
|---|---|---|
| CHEM101 Introduction to Chemistry | 3 | T2 |
| CS103 Discrete Structures | 2 | T2 |
| MATH101 Introduction to Mathematics | 1 | T2 |

73 sections rather than 76, 172 weekly meetings rather than 178. **Every one is
T2** — a shared foundation course taught in other sections right across the
college, which is precisely the category the tier system says a student can pick
up elsewhere. No T1 course fell below the floor, so no student lost access to a
specialised major course they can only take here; and **no student was left
without a timetable at all.**

**This is the one filter in the subsystem that makes every other number look
better.** Fewer sections to place, fewer pairs that can collide, fewer rooms to
find, less instructor idle time. Everything else here fails loudly and makes the
report worse; this one improves it by handing the solver a smaller problem. So it
is reported as a **WARNING that names every withheld course, its size and its
tier**, and students left with nothing are counted on their own line — they leave
the demand set entirely, and would otherwise flatter every per-student average by
disappearing from it rather than by being served.

`min_demand=1` disables the rule, and that is what `build_snapshot` itself
defaults to: the policy value of 5 lives at the callers (the Generate button, the
job runner, the management commands), exactly as `default_capacity` does, so no
test silently inherits a filter.

**The two things worth watching.** The floor is applied to *any* course, per the
owner's rule. If a **T1** course ever falls below it, the tier argument does not
hold for that course — nobody else in the college teaches it — and the withheld
students have no route to it at all. The readiness warning reports the tier of
every drop precisely so that case is visible the day it happens rather than
discovered by a student. Second, the reduction in search space is real but
modest: three sections out of 76. The rule earns its place by not spending a
scarce weekly cell on one student, not by making the solver faster.

---

### D19 — ENG101 and ENG102 own the morning, and their own rooms

> *"ENG101 and ENG102 are different — they always occupy the full morning, all
> the days of the week, so they are fixed, all sections at the same time, every
> day, all the morning slots."* … *"Keep them out of the rooming — we have
> special rooms for ENG101 and ENG102."*

ENG101/ENG102 ("English Language Skills I/II") are the intensive language pair:
4 credits, plan terms 1 and 2, carried by **all twelve programmes**. Their hours
are not a preference for the objective to pursue — they are the shape of the
course, so they are compiled into the requirement instead.

**The arithmetic is what makes it exact.** The meetings are confined to the
morning starts, and there are precisely as many meetings as there are
`(day, morning start)` cells: 2 × 5 = 10. One meeting per cell, ten meetings, ten
cells — the only feasible answer is *every cell filled every day*. **"All sections
at the same time" is therefore a consequence, not a further constraint.**

The block is **read from the grid, never written down** (D2). Taken greedily from
the earliest start, the declared grid gives 09:00–10:15 and 10:30–11:45; 10:50 is
dropped because it overlaps 10:30, and a section cannot be in two places at once.

**Five rules are suspended, each at the point it is imposed:**

| rule | why it cannot apply |
|---|---|
| **H2** one meeting per section per day | meeting twice in one morning is the entire point |
| **H10** siblings never overlap | the owner's rule *is* that every section sits at the same hour |
| **D14** a section keeps its hour | it uses the whole block by design |
| **D17** a pair owns a block and alternates | these own every slot of theirs — already stricter |
| sibling symmetry breaking | orders siblings by first cell; identical siblings cannot strictly increase |

Every one of those is a rule about **choice**. Nothing physical is suspended:
ENG is **in-person**, so the student's hour is occupied, the class still clashes,
the instructor is still teaching and still on campus.

**The rooms are the exception, and only the rooms.** The course has its own
space, so it draws nothing from the shared estate. That is expressed as
`uses_shared_room=False` on the requirement and deliberately **not** by flipping
the delivery mode to ONLINE, which would have been the shorter route and would
have silently removed the course from the clash model and from campus travel.
`needs_room` and `is_fully_online` keep their meanings.

It matters here more than it sounds: this cohort has **eight lecture rooms**, and
four ENG101 sections would otherwise have held **half of them in every morning
cell, five days a week**. Measured on the male AI/AI2/DS/DS2 cohort: the shared
estate now carries 152 room-consuming meetings rather than 192, and the room
shortfall is **unchanged at 19** — proving the shortfall was never ENG's doing.

**What it costs the rest of the board.** 80 of 390 students take ENG101, and for
every one of them the entire morning is spoken for — all their other courses must
fit the afternoon. The rule is named on **exact codes**, not on an `ENG` prefix:
ENGL103, ENGL104 and ENGL214 are ordinary three-credit English courses and keep
every ordinary rule.

---

### D20 — One half-day per curriculum term: the owner's method, and why it backfires

> *"If term 9 is placed in the morning I will try my best to make term 5
> afternoon, because an irregular student of 9 might need something from term 5.
> Then I place term 3 morning and term 1 afternoon. Then I start to modify each
> within its specified period to avoid the conflicts and reduce the gaps."*

Stated plainly, that is **strict alternation by term** — and it is a
**MAX-CUT**: terms are nodes, an edge weighs how many students take courses in
both, and the split cuts as much of that weight as possible. Solved exactly here
(the graph has five nodes), subject to each half fitting its own room supply.

**The owner's hand rule is optimal.** Computed independently from the live
demand, the solver returns the owner's partition exactly:

```
term 1  PM      term 3  AM      term 5  PM      term 7  AM      term 9  PM
```

218 of 278 shared student-pairs separated (78%) — and no other assignment does
better. The reason it works is structural: an irregular student is usually one
or two terms behind, so the heaviest edges are between **neighbouring** terms
(5+7 = 112 students, 3+5 = 36, 7+9 = 35), and alternation cuts every one.

**And measured on a real build it is clearly worse.** M cohort, 3 runs each:

| | D20 off | **D20 on** |
|---|---|---|
| expected clashes | **132.8** (118–166) | **242.2** (238–245) |
| instructor idle | 960 (715–970) | 950 (850–1305) |
| a section keeps its hour | 60% | 35% |
| …within one slot | 100% | 78% |
| days / unroomed | 19 = floor / 19 | 19 = floor / 19 |

**Student collisions nearly doubled** — by the rule written to prevent them.

**Why.** The clash objective prices every pair of courses one student holds at
once, and those pairs divide like this:

| | share |
|---|---|
| both courses in the **same** term | **74 %** |
| courses in **different** terms | 26 % |

D20 protects the 26% by crushing the 74%. Confining a term to half the day
collapses the slots its own courses can spread across:

```
term 7 (AM)   39 meetings into 10 distinct (day, cell) slots   -> 3.9 deep
term 3 (AM)   30 meetings into 10 slots                        -> 3.0 deep
unphased      a term may use all 25 slots
```

The morning is the worse half — the grid gives it two disjoint 75-minute cells
against the afternoon's three — so the terms sent there suffer most.

**It also breaks the instructors, and structurally rather than by accident.** An
instructor teaches courses in *adjacent* terms; alternation exists to put
adjacent terms in *opposite* halves. On the live data **4 of 5 linked
instructors** end up straddling noon — Dr Nawaf teaches AI225 (term 5, PM) and
DS321 (term 7, AM), a forced gap every week. The two goals pull the same lever
in opposite directions:

* a student spanning adjacent terms wants those terms **apart**;
* an instructor teaching adjacent terms wants them **together**.

**The deeper reading, and the reason this stays off.** The clash objective
*already* prices cross-term collisions — it counts every pair of courses sharing
students, whatever term they sit in. The solver was already doing what D20 tries
to teach it, and with the freedom of the whole day rather than half of it. The
owner's heuristic is an excellent way for a *human* to approximate that by hand,
because a person cannot evaluate three thousand booleans; imposing it on the
solver removes freedom it was using well. This is the same lesson as the
hand-written LNS neighbourhoods that lose to generic propagation-guided ones.

Shipped as `--phase-terms`, **default OFF**. The partition logic is kept and
tested — it is correct, it independently reproduces the owner's own rule, and it
is the right machinery should a *soft* version ever be worth measuring.

---

### On making the search better — what was tried, and what it was worth

The pinning experiment behind D17 also produced the clearest evidence we have
about where quality actually comes from. Fixing one instructor's eight meetings
to a hand-designed block and re-solving everything around it took total
instructor idle from **2715 to 940**, and left every other instructor's week the
same length or shorter. Nobody paid.

That prompted a wider search for levers. Most of them are worth nothing here, and
the negative results are recorded so nobody spends the time again:

| lever | verdict |
|---|---|
| **more CPU workers** (8 → 16 → 24) | **no effect.** T1 collisions 2.0 / 1.8 / 3.0 and idle 1660 / 1640 / 1260 by median, with fully overlapping ranges. 16 workers produced the single worst idle figure measured. The machine has 20 cores and CP-SAT cannot turn them into a better timetable on an instance this size. |
| **GPU** (RTX 4060 Ti) | **structurally unavailable.** CP-SAT is a SAT/CP engine — clause learning and propagation over irregular structures — and has no CUDA path. NVIDIA cuOpt now has a beta MIP solver whose strength is exactly ours (good feasible solutions, no optimality proof), but using it means restating every rule in a second formalism, which is the disease this subsystem exists to cure, for an instance of ~3,000 booleans where GPU kernels lose to a CPU. |
| **more time** (120 / 300 / 600 s) | **widens the spread rather than improving the median.** 600 s produced both the best result measured (895 min idle) and the worst (3330). No trend in collisions. |
| **hand-written LNS neighbourhoods** | **rejected on the literature.** Perron, Shaw & Furnon's *Propagation Guided Large Neighborhood Search* (CP 2004) reports generic propagation-guided neighbourhoods beating hand-written ones on both performance and stability — and Perron leads OR-Tools, with `use_lns`, `use_rins_lns` and `use_lb_relax_lns` all on by default in our runs. We would be hand-writing the thing that loses. |
| **portfolio selection** | the one that pays. Variance is the phenomenon, so harvesting it beats hoping for a good draw. |

**The state of the art has not moved.** The ITC 2019 winner — a parallelised
matheuristic: MIP plus fix-and-optimize, unfixing ~25% of assignments per
iteration, several searches on separate neighbourhoods resetting to the best
known solution and diversifying on stall — still stands five years on. PATAT 2024,
held at the winners' own institution, contains **no ITC 2019 paper at all**; the
field moved to healthcare timetabling, bus driver scheduling and nurse rostering.

**And a re-reading of our own result.** The Dr Nawaf experiment is better
understood as a *warm start* than as a neighbourhood: what helped was handing the
solver a high-quality partial solution designed by a human, not the fact that the
subset was structured. The ITC winner names "two methods for producing initial
solutions" as an ingredient in its own right. Which lands somewhere useful —
**D17 is that structure, made permanent and free.** The owner's policy rule and
the search improvement are the same thing.


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

# Timetable Constraints — Hard & Soft (authoritative reference)

Extracted from the code on **2026-07-24**. Every entry cites where it lives so it
can be re-verified. "Hard" = a violation is never accepted (the candidate is
rejected or repaired). "Soft" = it is priced into the objective and traded
against other soft terms.

**Scope note.** The builder decides only `(weekly pattern, day/period, room)` per
section. Section counts, section capacities, expected demand and instructor
assignment are frozen upstream. See the `docs/` cohort briefing for input shape.

---

## 1. Hard constraints

### 1.1 Structural / per-section

| # | Constraint | Rule | Where |
|---|---|---|---|
| H1 | **Meeting count & duration** | Driven by credit hours: 4cr → 3 meetings (75/75/100 min); 3cr → 2 meetings (75/75); 2cr → 1 meeting (100); 1cr → 1 meeting (75) | `timetable_autoplace.py` header; `infer_required_meeting_count` |
| H2 | **All-different-days** | No more than **1 meeting per day per section** | `timetable_autoplace.py` |
| H3 | **Legal slot grid** | Lectures start only at **09:00, 10:30, 10:50, 13:00, 14:30, 14:45, 16:00** (7 slots, 75 min); labs at **09:00, 10:45, 13:00, 14:45, 16:30** (5 slots, 100 min). Days SUN–THU. 10:30/10:50 and 14:30/14:45 are deliberate overlapping *post-lab* pairs — a section takes one or the other | `scenario.slot_config` / `lab_slot_config`, `DEFAULT_SLOTS` / `DEFAULT_LAB_SLOTS` in `timetable_autoplace.py` |
| H4 | **Blocked slots** | Scenario `blocked_slots` cells are never used | `blocked_slot_keys()` in `timetable_validation.py` |
| H5 | **Prayer compliance** | No lecture starts in **11:30–12:59**, no lab in **11:10–12:59**. **NOT enforced in code** — holds only by curated grid start times; a hand-edited `slot_config` in a prayer window is *not* caught by anything | `DEFAULT_SLOTS` / `DEFAULT_LAB_SLOTS` |
| H6 | **Locked placements** | `is_locked` placements are never moved or deleted | gated by `TIMETABLE_ENFORCE_LOCKS` (default **true**) |

### 1.2 Resource exclusivity

| # | Constraint | Rule | Where |
|---|---|---|---|
| H7 | **Instructor clash** | An instructor is never double-booked. **Interval-aware** (not exact-start): a 10:30–11:45 lecture and a 10:45–12:25 lab *do* conflict | `has_instructor_clash` / `count_instructor_clashes` / `move_introduces_instructor_clash` / `list_instructor_clashes` — `timetable_constraints.py`; flag `TIMETABLE_PR4_INSTRUCTOR_CLASH_ENABLED` (**true**) |
| H8 | **Instructor daily cap** | An instructor teaches **≤ 3 sessions/day** (lectures *and* labs count). A 4th is forbidden; the cap **wins against students** | `exceeds_instructor_daily_cap` / `count_instructor_daily_overloads` / `move_exceeds_instructor_daily_cap`; flags `TIMETABLE_INSTRUCTOR_DAILY_CAP_ENABLED` (**true**), `TIMETABLE_INSTRUCTOR_DAILY_CAP` (**3**) |
| H9 | **Room exclusivity** | No two meetings share a room at overlapping times | `detect_board_conflicts` → `room_clashes`; persist-time checks |
| H10 | **Same-course separation** | Sibling sections of one course must **not overlap** in time | `has_same_course_overlap` / `move_introduces_same_course_overlap` — `timetable_constraints.py`, `timetable_same_course.py` |

### 1.3 Room compatibility

| # | Constraint | Rule | Where |
|---|---|---|---|
| H11 | **Room type** | A lab meeting requires a `room_type = "lab"` room; lectures use lecture rooms | `timetable_rooming.py` pools |
| H12 | **Room capacity** | `room.capacity >= section demand`, where demand is the **budgeted section capacity × capacity buffer** (`TIMETABLE_CAPACITY_BUFFER`, default **1.1**) — *not* actual enrolment | `assign_best_fit`, `get_capacity_buffer()` |
| H13 | **Room gender** | Room `section` (M/F) must match the board/section gender | `_section_gender`, `get_board_gender`, `check_gender_feasibility` |
| H14 | **Room department policy** | Room's comma-separated `department` must contain the section's programme | `timetable_rooming.py` (`room_progs` match) |

### 1.4 Student-facing hard rules

| # | Constraint | Rule | Where |
|---|---|---|---|
| H15 | **Critical student overlap** | Two sections at the same time are **critical** if they are same-course *or* share **≥ 20 students** (`HARD_OVERLAP_THRESHOLD`); critical conflicts block publish | `timetable_overlap.py`, `detect_board_conflicts` |
| H16 | **Section capacity** | A section never exceeds `max_capacity`; `reserve_capacity` is held back so `regular_limit = max_capacity − reserve_capacity` | `SectionState` in `timetable_assignment_models.py` |

> **Publish gate:** board-local critical overlaps / instructor clashes, scenario-wide
> cross-board H15 conflicts at the `>= 20` threshold, unassigned physical rooms,
> and scenario-wide H11–H14 room incompatibilities block publication. Room clashes
> (H9) are also blockers whenever physical rooms are assigned.

---

## 2. Soft constraints (the objective)

Optimised **lexicographically** — position 0 dominates position 1, and so on.
Produced in one place: `evaluate_assignability_lexicographic`
(`timetable_student_assignment.py`). Decode with `decode_score()`, never by fixed
index.

### 2.1 Tiered objective — 9-tuple (default; `TIMETABLE_TIERED_OBJECTIVE_ENABLED` = **true**)

| Pos | Term | Meaning |
|---:|---|---|
| 0 | **High-risk unresolved** | A `RiskTier.A` (retake) student with an unresolved T1/T2 course — drive to 0 |
| 1 | **Student clashes** | Time collisions in the seated student schedules |
| 2 | **Tier-1 unresolved** | Unresolved (student, course) pairs for **core major** courses — hard, drive to 0 |
| 3 | **Tier-2 over tolerance** | Shared-foundation unresolved **beyond** `TIMETABLE_TIERED_T2_TOLERANCE` (default **3** per course) |
| 4 | **Student cost** | `real_gap_minutes + TIMETABLE_TIERED_SOFT_GAP_BUDGET × soft_unresolved` (budget default **120**) — a **bounded trade**, not strict priority |
| 5 | **Soft count** | Tier-3 (gen-ed) + Tier-2 within tolerance |
| 6 | **Reserve used** | Reserve seats consumed |
| 7 | **Same-course spread** | Sibling-section spread quality (its own term, unbundled from gap) |
| 8 | **Instructor idle** | Instructor idle minutes (only when `TIMETABLE_INSTRUCTOR_GAP_PENALTY_ENABLED`) |

Real gap is recoverable as `score[4] − budget × score[5]`. Budget `0` reproduces
strict quality-first.

**Course tiers** (`timetable_course_tier.py`): **T1** = specialised major course
(in ≤ 2 plans); **T2** = shared foundation (MATH/STAT prefix, or in > 2 plans);
**T3** = gen-ed (ENGL, GS, GSE, FE prefixes). Tiers are load-bearing on the
*seating order*, not only the score — core is seated first.

### 2.2 Legacy objective — 6-tuple (`TIMETABLE_TIERED_OBJECTIVE_ENABLED` = false)

```
(tier_a, unresolved_students, unassigned_courses, clashes,
 gap_minutes[+spread folded in], reserve)  [, instructor_idle]
```

### 2.3 Online sessions are excluded from gap chains

**Rule.** An ONLINE session is attended remotely, so it neither creates nor
bridges an on-campus gap. It is excluded from:

- the **student** day-gap chain (`_compute_total_state_gap`),
- the **candidate pricing** used while seating (`calculate_added_gap` — an online
  candidate adds 0 gap and never anchors the chain),
- the **instructor** idle chain (`_compute_instructor_idle_minutes`).

A day containing only online sessions therefore scores **zero** campus gap. Online
teaching still counts as a teaching session / working day for the instructor and
remains fully subject to H7/H8 — the exclusion is only for physical-campus span/idle.

**Implementation.** `SectionState.is_online` is resolved once, per board, when
states are built (`build_section_states_for_scenario`) via
`OnlineCourseLookup.is_online_course_for_board(board, course_code)` on the **bare**
`TermSection.course_code`. The gap hot loops read a bool through
`_campus_meetings()`. The CP-SAT polisher copies the marker through, or a polished
board would be rescored as if every online session were on campus.

**Flag.** `TIMETABLE_ONLINE_GAP_EXCLUSION_ENABLED`, default **true**. With it off,
no section is ever marked online, reproducing the previous online-blind scoring
**byte-for-byte** (verified: scn 643 flag-off = 112,330). Measured on scn 643
(AI/AI2/DS/DS2 M 1448-T1): 6 of 70 sections online; student cost 112,330 → 87,060;
real gap 105,610 → 80,340 — **−23.9%** of the reported gap was phantom.

> **Not covered yet:** attendance-day counts still treat an online-only day as an
> attendance day. If that metric starts driving decisions, apply the same
> exclusion there.

### 2.4 Other soft preferences

| Term | Rule | Where |
|---|---|---|
| **Same-course same-day preference** | Sibling sections *prefer* the same day; a spread penalty prices deviation | `same_course_section_spread_penalty` |
| **Back-to-back detection** | Adjacent sibling meetings scored separately | `is_back_to_back_gap`, `has_back_to_back_pair` |
| **Midday-break gap penalty** | A student day that straddles the midday break (has morning **and** afternoon meetings) with ≥ 60 min idle is **double-penalised**. Keyed to `MIDDAY_END = 13:00` — a scheduling-quality heuristic, **not** the prayer rule | `_score_option` in `timetable_autoplace.py` |
| **Warning-level overlap** | Sections sharing **1–19** students — flagged, does **not** block publish | `HARD_OVERLAP_THRESHOLD = 20` |
| **Slot-position preference** | Per-meeting penalty by start time (`_SLOT_PENALTY_BY_START`) + a density term; online courses instead penalise *early* slots | `_score_option` in `timetable_autoplace.py` |

### 2.5 Instructor schedule compaction (opt-in post-pass)

A post-build pass that minimises fair instructor **excess working days** first,
then physical-campus span/idle, with layered student and room-safety guards.
Flag `TIMETABLE_INSTRUCTOR_COMPACTION_ENABLED` (default **false**). It relocates
sessions in time only — it never changes who teaches what.

- **Working-day lower bound** per instructor: `max(ceil(session_count / daily_cap),
  largest meeting count of an assigned section)` (the second term is required by
  H2). Its fair lexicographic objective is `(maximum excess days, total excess
  days, physical span/idle, student-gap tie-breaker)`. All teaching sessions
  (including online) count toward working days and H7/H8; only physical sessions
  enter campus span/idle. Does not change the canonical 9-tuple above.
- **Fail-closed persistence.** Accepted moves vacate their unique keys before final
  writes (cyclic updates are safe), rerun rooming inside the same transaction, and
  rebuild `TermSectionMeeting` from `SectionPlacement`. Every moved physical
  placement is re-checked against H11–H14 (buffered H12 capacity applies to labs
  too). If greedy rooming fails H9/H11–H14, a deterministic single-worker CP-SAT
  fallback solves the moved physical meetings — then, only if needed, their
  overlap-connected component so room swaps can release a feasible room; objective
  `(room-code changes vs pre-pass board, total capacity waste)`. Online meetings
  stay roomless. Any increase in H15/H7/H8/scenario-wide H9/unassigned rooms, an
  unproven/infeasible repair, a mapping mismatch, or any DB/rooming error rolls the
  **entire** pass back.

> The scenario-wide H9/H15 persistence audits use a day-grouped interval sweep
> (expired intervals leave the active heap before candidate pairs are checked).
> Verified against the former quadratic oracle (20 seeds × 80 intervals) and O(n)
> on a 10,000-row non-overlap regression.

---

## 3. Priority order (what wins against what)

1. Hard feasibility (§1) — never traded.
2. **Instructor daily cap wins against students** — a section may go unplaced rather than create a 4th same-day session.
3. High-risk unresolved → student clashes → Tier-1 → Tier-2-over-tolerance.
4. Student cost (gap ⟷ soft enrolment, **bounded** trade).
5. Soft count → reserve → same-course spread → instructor idle.

The mandatory instructor repairs (cap, clash) are **exempt from both rollback
gates** — they are judged against `score_before_instructor_passes` /
`safety_before_instructor_passes`, so a required repair cannot be vetoed and then
reinstate the violation it just cleared.

---

## 4. Feature flags that gate constraints

| Flag | Default | Gates |
|---|---|---|
| `TIMETABLE_ENFORCE_LOCKS` | **true** | H6 locked placements |
| `TIMETABLE_PR4_INSTRUCTOR_CLASH_ENABLED` | **true** | H7 instructor clash |
| `TIMETABLE_INSTRUCTOR_LINKS_ENABLED` | **true** | per-section instructor identity (prerequisite for H7/H8) |
| `TIMETABLE_INSTRUCTOR_DAILY_CAP_ENABLED` | **true** | H8 daily cap |
| `TIMETABLE_INSTRUCTOR_DAILY_CAP` | **3** | cap value |
| `TIMETABLE_TIERED_OBJECTIVE_ENABLED` | **true** | 9-tuple objective |
| `TIMETABLE_TIERED_T2_TOLERANCE` | **3** | position 3 tolerance |
| `TIMETABLE_TIERED_SOFT_GAP_BUDGET` | **120** | position 4 trade |
| `TIMETABLE_CAPACITY_BUFFER` | **1.1** | H12 room sizing |
| `TIMETABLE_ONLINE_GAP_EXCLUSION_ENABLED` | **true** | online exclusion from gap terms (§2.3) |
| `TIMETABLE_INSTRUCTOR_GAP_PENALTY_ENABLED` | **false** | position 8 (proven a no-op — see §5) |
| `TIMETABLE_INSTRUCTOR_COMPACTION_ENABLED` | **false** | workday-first instructor compaction pass (§2.5) |

There is **no** flag for H5 prayer — it is enforced by nothing (see §1.1 / §5).

---

## 5. Known gaps — constraints we do **not** model

Be explicit about these; they are data/design limits, not solver bugs.

- **Prayer compliance is unenforced.** It holds only because the shipped
  `DEFAULT_SLOTS` / `DEFAULT_LAB_SLOTS` start times avoid the prayer windows. A
  hand-edited or imported non-compliant `slot_config` would schedule straight
  through prayer time with no warning. Grid curation is the sole safeguard.
- **Instructor personal availability** — no per-instructor unavailable/preferred
  periods exist. Only occupancy (H7), the daily cap (H8) and scenario-wide blocked
  slots apply. "Instructor X can't teach Sunday" is **not** expressible.
- **Arbitrary room features** — room compatibility is type + capacity + gender +
  department + building only. No equipment/accessibility tags.
- **Walking distance / campus travel** — building/wing/floor exist but are not
  optimised (most rooms lack location metadata).
- **Student→section membership is not an input** and is not persisted. The
  evaluator performs an **ephemeral, greedy, risk-tier-ordered seating** to score a
  board; per-student clash/gap numbers are real *under that seating* but are
  neither globally optimal nor saved.
- **Instructor coverage is sparse** — in the AI/AI2/DS/DS2 M 1448-T1 cohort only 5
  course-rows (3 instructors, 25 of 168 meetings) carry an instructor, so H7/H8 and
  compaction bind on that slice only. An apparently compact result is not evidence
  about the unmapped meetings.
- **Position 8 (instructor idle) is effectively inert** — measured on scn 627 it
  decided **0 of 6,661** moves, because a term last in a lexicographic tuple only
  breaks exact ties. Real instructor compaction requires the dedicated pass (§2.5).
- **Room assignment is per-board.** Construction cannot coordinate the shared room
  pool globally; the scenario-wide publish backstop *rejects* the resulting
  H9/H11–H14 failures but does not repair them. Manual/bulk placement writers do
  **not** enforce H8.

---

## 6. Enforcement points

| Stage | Enforces |
|---|---|
| Greedy (`timetable_autoplace`) | H1–H8, H10–H14 as candidate filters |
| V2 local / chain search | H7, H8, H10 per move (delta form) |
| CP-SAT polisher | H7, H8, H10 natively |
| SA polish (`timetable_local_search`) | H7, H8 (board-scoped — blind to cross-board) |
| Rooming (`timetable_rooming`) | H9, H11–H14 (**per board**, not global) |
| Repair passes | cap repair (H8, relocate-never-drop), clash repair (H7) |
| Persist-time backstop | H7 scenario-wide across every writer (observability; enforcement only at the V2 gate) |
| Publish gate | Board-local critical conflicts, cross-board H15 at `>= 20`, assigned-room clashes, unassigned physical rooms, scenario-wide H11–H14 compatibility |

> **Note:** H5 (prayer) appears in **no** enforcement stage — it is a property of
> the curated grid data, not a checked rule.

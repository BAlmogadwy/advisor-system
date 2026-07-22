# Timetable Constraints — Hard & Soft (authoritative reference)

Extracted from the code on 2026-07-22. Every entry cites where it lives so it can
be re-verified. "Hard" = a violation is never accepted (the candidate is rejected
or repaired). "Soft" = it is priced into the objective and traded against other
soft terms.

**Scope note.** The builder decides only `(weekly pattern, day/period, room)` per
section. Section counts, section capacities, expected demand and instructor
assignment are frozen upstream. See `docs/` cohort briefing for input shape.

---

## 1. Hard constraints

### 1.1 Structural / per-section

| # | Constraint | Rule | Where |
|---|---|---|---|
| H1 | **Meeting count & duration** | Driven by credit hours: 4cr → 3 meetings (75/75/100 min); 3cr → 2 meetings (75/75); 2cr → 1 meeting (100); 1cr → 1 meeting (75) | `timetable_autoplace.py` header; `infer_required_meeting_count` |
| H2 | **All-different-days** | No more than **1 meeting per day per section** | `timetable_autoplace.py` |
| H3 | **Legal slot grid** | Lectures start only at 09:00, 10:30, 10:50, 13:00, 14:30, 16:00; labs at 09:00, 10:45, 13:00, 14:45, 16:30. Days SUN–THU | `scenario.slot_config` / `lab_slot_config`, `DEFAULT_SLOTS` |
| H4 | **Blocked slots** | Scenario `blocked_slots` cells are never used | `blocked_slot_keys()` in `timetable_validation.py` |
| H5 | **Prayer compliance** | No lecture starts in **11:30–12:59**, no lab in **11:10–12:59**. Guaranteed *at the grid level*, not per meeting | `LECTURE_PRAYER_BLOCK` / `LAB_PRAYER_BLOCK`, `assert_slot_grid_prayer_compliant` |
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

> **Publish gate:** `critical = len(overlaps) + len(instructor_clashes)`;
> `warning = len(room_clashes)`. Critical blocks publication.

---

## 2. Soft constraints (the objective)

Optimised **lexicographically** — position 0 dominates position 1, and so on.
Produced in one place: `evaluate_assignability_lexicographic`
(`timetable_student_assignment.py`). Decode with `decode_score()`, never by
fixed index.

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

### 2.3 Other soft preferences

| Term | Rule | Where |
|---|---|---|
| **Same-course same-day preference** | Sibling sections *prefer* the same day; a spread penalty prices deviation | `same_course_section_spread_penalty` |
| **Back-to-back detection** | Adjacent sibling meetings scored separately | `is_back_to_back_gap`, `has_back_to_back_pair` |
| **Instructor day compaction** | Post-build pass shrinking within-day instructor idle gaps, with layered student guards | `timetable_instructor_compaction.py`; flag `TIMETABLE_INSTRUCTOR_COMPACTION_ENABLED` (**false**) |
| **Warning-level overlap** | Sections sharing **1–19** students — flagged, does **not** block publish | `HARD_OVERLAP_THRESHOLD = 20` |

> ✅ **Online sessions are excluded from gap terms** (positions 4 and 8). An online
> class is attended remotely, so it neither creates nor bridges a campus gap.
> Gated by `TIMETABLE_ONLINE_GAP_EXCLUSION_ENABLED` (**true**) — see §2.4.

---

## 3. Priority order (what wins against what)

1. Hard feasibility (§1) — never traded.
2. **Instructor daily cap wins against students** — a section may go unplaced rather than create a 4th same-day session.
3. High-risk unresolved → student clashes → Tier-1 → Tier-2-over-tolerance.
4. Student cost (gap ⟷ soft enrolment, bounded trade).
5. Soft count → reserve → same-course spread → instructor idle.

The mandatory instructor repairs (cap, clash) are **exempt from both rollback
gates** — they are judged against `score_before_instructor_passes` /
`safety_before_instructor_passes` so a required repair cannot be vetoed and
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
| `TIMETABLE_INSTRUCTOR_GAP_PENALTY_ENABLED` | **false** | position 8 (proven a no-op — see below) |
| `TIMETABLE_INSTRUCTOR_COMPACTION_ENABLED` | **false** | instructor compaction pass |

---

## 5. Known gaps — constraints we do **not** model

Be explicit about these; they are data limits, not solver limits.

- **Instructor personal availability** — no per-instructor unavailable/preferred
  periods exist. Only occupancy (H7), the daily cap (H8) and scenario-wide
  blocked slots apply. "Instructor X can't teach Sunday" is **not** expressible.
- **Arbitrary room features** — room compatibility is type + capacity + gender +
  department + building only. No equipment/accessibility tags.
- **Walking distance / campus travel** — building/wing/floor exist but are not
  optimised (most rooms lack location metadata).
- **Student→section membership is not an input** and is not persisted. The
  evaluator performs an **ephemeral, greedy, risk-tier-ordered seating** to score
  a board; per-student clash/gap numbers are real *under that seating* but are
  neither globally optimal nor saved.
- **Instructor coverage is sparse** — in the AI/AI2/DS/DS2 M 1448-T1 cohort only
  5 course-rows (3 instructors, 25 of 168 meetings) carry an instructor, so H7/H8
  bind on that slice only.
- **Position 8 (instructor idle) is effectively inert** — measured on scn 627 it
  decided **0 of 6,661** moves, because a term last in a lexicographic tuple only
  breaks exact ties. Real instructor compaction requires the dedicated pass (§2.3).

### ~~Online courses counted as campus gap time~~ — **FIXED 2026-07-22** (moved to §2.4)

Kept here only as history; the live description is §2.4.

**Rule:** an **online** meeting must **not** create campus idle time.
A student attending an online class remotely has no on-campus gap before or after
it, so the minutes around it should not be charged to `gap_minutes`, and the
online meeting should not be treated as an on-campus anchor that "fills" a day.
The same applies to instructor idle (§2.3) and to attendance-day counts.

**Behaviour BEFORE the fix (historical):** the gap computation was online-blind:

- `_compute_total_state_gap()` collects **every** meeting of every seated
  section per day, and `_compute_day_gap()` sums
  `next.start_min − prev.end_min` across all of them, with **no online filter**
  (`timetable_student_assignment.py`).
- `timetable_student_assignment.py` — the module that owns **both** the seating
  and the objective — contains **zero** references to online courses, while
  `OnlineCourseLookup` is used in ~10 other modules (`timetable_rooming`,
  `timetable_autoplace`, `timetable_solver`, `timetable_workspace`,
  `timetable_repair`, …). The concept exists everywhere *except* the scorer.
- `SectionMeeting` carries **no online flag** (`day`, `start_min`, `end_min`,
  `slot_size`, `mask`), so the evaluator has no way to tell the two apart even
  in principle.

**Consequences**

1. `gap_minutes` (tuered position 4 / legacy position 4) is **overstated** for
   every student holding an online course, so the optimiser pays real on-campus
   quality to close gaps that do not exist.
2. It can actively pick a **worse** board: moving an in-person class to sit
   "next to" an online one scores as a gap reduction while changing nothing for
   the student.
3. Same distortion applies to instructor idle and to attendance/campus-day
   metrics.

In the AI/AI2/DS/DS2 M 1448-T1 cohort this was not hypothetical: **6 of 70
sections** are online (the meetings intentionally carrying no room).

---

## 2.4 Online sessions are excluded from gap chains ✅

**Rule.** An ONLINE session is attended remotely, so it neither creates nor
bridges an on-campus gap. It is excluded from:

- the **student** day-gap chain (`_compute_total_state_gap`),
- the **candidate pricing** used while seating (`calculate_added_gap` — an online
  candidate adds 0 gap, and an online section never anchors the chain),
- the **instructor** idle chain (`_compute_instructor_idle_minutes`).

A day containing only online sessions therefore scores **zero** campus gap.

**Implementation.** `SectionState.is_online` is resolved **once** when states are
built (`build_section_states_for_scenario`) from
`OnlineCourseLookup.codes_for_programmes(scenario.programs)`, matched on the
**bare** `TermSection.course_code` — *not* the planner `CODE::NAME` identity the
`SectionState` carries. The gap hot loops then just read a bool via
`_campus_meetings()`. The CP-SAT polisher copies the marker through, or a
polished board would be rescored as if every online session were on campus.

**Flag.** `TIMETABLE_ONLINE_GAP_EXCLUSION_ENABLED`, default **true**. With it off
no section is ever marked online, so the previous online-blind scoring is
reproduced **byte-for-byte** (verified: scn 643 flag-off = 112330, the historical
value).

**Measured impact (scn 643, AI/AI2/DS/DS2 M 1448-T1):**

| | flag off | flag on |
|---|---:|---:|
| Sections marked online | 0 | 6 |
| Student cost (pos 4) | 112,330 | 87,060 |
| **Real gap minutes** | **105,610** | **80,340** |

**−25,270 minutes (−23.9%)** of the reported student gap was phantom. Positions
0–3 (feasibility/tiers), soft count, reserve and same-course spread are all
unchanged — the fix is surgical.

> **Not covered yet:** attendance-day counts still treat an online-only day as an
> attendance day. If that metric starts driving decisions, apply the same
> exclusion there.

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
| Publish gate | H15 critical conflicts block |

> **Known weakness:** rooming is **per-board**, so it cannot coordinate the shared
> room pool globally, and it sizes to *budgeted capacity × buffer* rather than
> actual enrolment. Both together are why a build can leave physical meetings
> unroomed. Manual/bulk placement writers do **not** enforce H8.

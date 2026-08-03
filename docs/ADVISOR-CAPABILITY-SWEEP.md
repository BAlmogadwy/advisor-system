# What the app can actually answer — a sweep of every screen and its internals

**2026-08-01.** Seven agents read the app slice by slice and *ran* what they read; two more
tried to knock the results down. 83 candidate capabilities found, 47 rejected, ~11 survive.

The sweep exists because an earlier judgement was wrong. An evaluation set built against
the adviser's 13 registered capabilities concluded many student questions were
unanswerable. It had measured what is **wired**, not what the app can **do** — and those
are very different. Filing a wiring gap as a data gap makes it permanent and invisible.

But the correction has its own failure mode, and the adversarial pass found it: assuming
that because a function exists, it reaches students. Mostly it does not reach as far as it
looks.

---

## Six measurements that bound everything below

**1. The section catalogue covers a third of the curriculum.**
`ProgrammeRequirement` holds 246 distinct course codes. Only **77 have any real section**.
169 have none — including the entire AI core (AI113, AI221, AI331, AI433, AI491). Per
programme it splits sharply: AI 29/50, CS 31/49, IS 28/51, COE 32/53, but **AI2 14/48, CS2
15/47, DS2 14/49, CYP2 14/49**. Any section-shaped answer works for the older programme
families and mostly fails for the newer ones.

**2. `TermSection` has no `academic_year` and no `term` field at all.**
Fields are `id, scenario, source_tag, course_name, available_capacity, registered_count,
course_code, course_number, course_key, section, source_file, created_at, updated_at`. The
`year`/`term` arguments on `_catalog_for_courses` are decoration — the query filters only
on `scenario__isnull=True` and `course_key`. **No capability in this app can honestly say
"الترم الجاي".** The truthful phrasing is "the sections we hold on file".

**3. The two terms are SEQUENTIAL, not conflicting — I had this backwards.**
Students have just completed **1447 term 2**, so the term they are about to register for is
**1448 term 1** — which is exactly what the calendar covers. `StudentTermSection` holding
1447/2 while recommendations compute at 1448/1 is the *correct* advising situation: here is
what you took, here is what comes next. `calculate_real_student_term` confirms it — asked
about 1447/2 it plans for the following term.

An earlier draft of this document called that a mismatch and treated it as a defect. It is
not. The only real requirement is that an answer **says which term it means**: "your current
timetable (1447/2)" versus "next term (1448/1)".

**3b. Section coverage is a DATA gap, not a design limit.**
Feeding the real 1448/1 recommendations into the catalogue: of 500 recommended course-slots
only **169 (34%)** have a same-gender section, and only **6 of 102** students can be given a
complete timetable. But the section data for 1448/1 is **incomplete — not yet loaded**, not
absent by design. So these numbers measure today's import, and they will move when the
sections for the coming term arrive. Capabilities should therefore be built for the complete
case and be explicit when a course has no sections on file, rather than being scoped down to
today's coverage. `build_plans` is real and fast (0.13s, 9 options); what it needs is an
honest "not on file" branch, not a smaller ambition.

**4. A segregation leak nobody had noticed.**
**722 of 3,807** student ids in `StudentTermSection` have **no `Student` row**.
`student_gender()` reads `Student.section`, so for those it returns `''` — and
`gender_section_filter('')` is an **all-pass** `Q()`. All 718 section labels are M or F
(415/303), so the ungendered branch is dead and the failure is total, not partial. A
wrapper must **refuse when gender is unknown**, never default.

**5. `course_code` means two different things.**
Real (scraped) sections store the department prefix — `course_code='AI'`,
`course_number='342'`, `course_key='AI342'`. Scenario-generated sections store the full code
in all three. So `filter(course_code='CS111')` returns **only generated** sections and
silently misses every real one. This is how I earlier reported "29 of 172 CS111 sections are
clash-free" — 172 generated sections; CS111 has **zero real ones**.
`group_availability._load_meetings_by_student` hits the same trap and labels the occupying
course `'CS'` instead of `'CS323'` — **wrong on 718 of 718 rows**.

**6. The exam data is not wireable yet.**
`ExamTimetableRun` has four fields and no published/active flag, `result_json` is a JSON
*string*, the two newest runs are labelled `jhasdhjashj`, their day vocabulary is
`W1-Sun`/`W2-Mon` with **no dates**, and their room labels intersect real
`TermSection.section` values in **zero** places. Choosing "the" exam schedule is a
data-model decision, not a capability.

---

## Defects in shipped code, found by running it

These are not eval-set issues. They affect the adviser today.

| Defect | Evidence |
|---|---|
| `get_student_term_baseline` emits **one row per meeting**, so any naive credit sum multi-counts. A 4-credit course meeting 3×/week reads as 12. | Observed directly: CS323/F8 appears 3× with `credits: 4` |
| `course_prerequisites` reports **no prerequisites for elective placeholders**, which is false — the real course behind the slot has them | swept and confirmed |
| `consider_capacity` is a **dead lever**: `available_capacity` is NULL on **0 of 718** rows, coerced to 0, so the CP-SAT capacity term is `sum(0·var)`. The adviser must never say "this section has seats". | measured |
| `swap_suggestions` are **placeholder strings** — `from_section='(current)'`, `to_section='(suggest alternative baseline section)'` emitted verbatim | `planner_builder.py:1320-1330` |
| `strict_per_course=True` is unusable on real data — returned `scheduled=0` for a real student's own 6 recommended courses | ran it |

---

## What to build — where both passes converged

Ranked by questions unlocked per unit of effort. **Four are fixes to capabilities that
already exist**, and those are the cheapest wins because they add no tool and so do not make
tool selection harder.

### Fixes to registered capabilities (no new tools)

1. **`course_prerequisites` — elective placeholders.** Currently answers "no prerequisites"
   for a slot whose real course has them. A wrong answer, not a missing one.
2. **`my_timetable` — registered-but-unscheduled courses, and a correct credit total.**
   Fixes the row-per-meeting multi-count above.
3. **`graduation_progress` — four fields already computed and dropped**:
   `final_term_possible`, `passed_credits_in_plan`, `registered_credits_now`, `in_progress[]`.
4. **`why_course_locked` — the forward direction.** `build_unlock_report` already returns a
   `graph` key that is thrown away; it answers "if I pass this, what opens?"

### New capabilities

5. **`my_plan_by_term`** — the plan level by level, each course marked passed / studying /
   open / locked with the missing prerequisite named. `_build_student_plan_payload(student_id)`
   takes only a student id, so it is student-safe by construction. *Trivial.*
6. **`my_clash_free_sections`** — for a named course, which sections fit the student's current
   timetable and which collide, naming the offending course, section, day and both time
   ranges. `_choose` already produces exactly that prose. *Moderate.*
7. **`build_my_timetable`** — wire `build_plans` (the **service**, never `planner_build_view`,
   which is `_require_staff` + throttled). Must ship with the partial-answer contract from
   measurement 3. *Moderate.*
8. **`my_electives`** — turn placeholder slots (AI1, CS3, FE2, GSE1) into real named courses.
   *Moderate.*
9. **`my_advisor`** — name and department rather than a bare id. ~3 lines. *Trivial.*

### Not now

**`my_exam_schedule`** — blocked on an owner decision about which run is authoritative, not
on code. See measurement 6.

---

## Three rules any wrapper must obey

1. **Refuse when gender is unknown.** Never let `gender=''` reach a section query — it is an
   all-pass filter and leaks the other cohort. 722 students are affected today.
2. **Query real sections by `course_key`**, never by `course_code`. See measurement 5.
3. **Always name the term, and never imply the catalogue is complete.** `TermSection`
   itself is termless, so a section list cannot claim to be "next term's" — but the student's
   own registrations ARE 1447/2 and the planning term IS 1448/1, and an answer must say
   which it is talking about. When a course has no sections on file, say exactly that:
   "not on file" is true, "no sections available" is not.

---

## What this changes about the evaluation set

The set marked these questions unanswerable on the strength of the registry. That was
wrong, and `NOT_WIRED` now exists to distinguish "the function is one wrapper away" from
"the data does not exist".

A third category is now needed and does not yet exist: **`DATA_NOT_LOADED`** — the function
works, the wiring is possible, and the *particular records* are simply not imported yet.
Section coverage is the whole of it. Grading those as permanently unanswerable would bake
today's import into the expectations, and they would silently stay wrong once the 1448/1
sections land.

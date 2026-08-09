# Student Advisor V2

## Product contract

V2 is one conversational academic adviser for the authenticated student. It is
not an intent-family or role router. The model decides which verified academic
evidence it needs, calls a deliberately small read-only capability surface, and
combines the results into one answer.

The adviser can:

- explain the student's academic state and degree progress;
- explain prerequisites, blockers, and course unlock impact;
- recommend and compare courses for a future study proposal;
- explain graduation outlook and approved university policy;
- identify the student's recorded academic adviser.

## Hard boundary

This application is not the university registration portal. V2 cannot:

- register or drop a real course;
- reserve a seat;
- save or apply a university timetable;
- change a section mapping;
- confirm that a portal action succeeded.

It may prepare and display a proposal. The student chooses whether to reproduce
that proposal manually in the university's main portal.

## First capability surface

The first slice reuses existing audited, read-only implementations:

- `get_student_context`
- `my_progress`
- `my_plan_by_term`
- `lookup_course`
- `course_prerequisites`
- `why_course_locked`
- `recommend_courses`
- `graduation_progress`
- `policy_lookup`
- `my_advisor`

The next read-only slice adds `my_timetable`, `my_clash_free_sections`, and
`build_timetable_proposal`. The last capability calls the existing deterministic
planner engine and returns multiple section/time alternatives directly to the
same agent loop. The legacy `build_my_timetable` tool remains excluded.

## Timetable proposal workspace

Timetable planning uses the same deterministic core from both chat and the
expanded companion screen. It is not another chatbot role and exposes no
write-capable tool. Both surfaces reuse the existing `/planner/` core:

- the same student context, recommendations, section catalogue and current-term
  baseline;
- the same credit policy and `planner_builder` solver;
- the same shared weekly-grid renderer;
- shortlist editing and optional section pins;
- build-around-current and propose-from-scratch modes;
- distinct visual alternatives with days, earliest start and latest end;
- explicit reasons for courses the solver could not place;
- a copyable course/section checklist for manual use in the university portal.

The student-facing adapter removes the staff planner's student-id input,
capacity override, swap diagnostics and Apply operation. The workspace is a
short-lived draft for generation idempotency, not a saved timetable. There is no
endpoint that can select, save, register, apply, reserve or alter a real course.

In chat, timetable-build requests are evidence-gated: the agent must call
`build_timetable_proposal` before answering. This prevents a generic
recommendation response from claiming that section times or clash detection are
unavailable when the application has both.

## Graduation forecast

`graduation_progress` extends the existing progress report with a read-only,
term-by-term scenario. It starts from the exact current Planner baseline, assumes
those courses pass, and repeatedly runs the existing recommender one main term
ahead. A simulated course is added to the in-memory passed set only after its
term, and every term is capped at 18 credits.

The scenario never writes a pass, registration, section, or timetable record.
It is an estimate only: every course is assumed passed on the first attempt and
future offerings, seats, section times, and registration permission are unknown.
If the recommender cannot resolve all plan requirements, the capability returns
a lower bound and the exact remaining prerequisite/hour blockers instead of an
invented completion term.

The same capability supports read-only changes to the current term. For a
specific question it removes and/or adds the requested course codes to an
in-memory copy of the Planner baseline, validates recorded prerequisites and the
18-credit scenario cap, then reruns the complete forecast. The response compares
the original and modified paths, including any term difference, resolved or
introduced blockers, and the future term into which a removed plan course is
deferred. A course outside the degree plan may unlock a plan requirement, but is
never counted as a completed plan course.

For an open-ended “is there a better replacement?” question, the capability runs
a bounded search over recorded, prerequisite-ready candidates and returns only
one-for-one changes with a demonstrable academic improvement. “Improved blockers”
is not presented as “earlier graduation” unless both simulations complete and
the term counts establish that result. This search does not establish section
availability, timetable compatibility, seats, or registration permission; those
are separate Planner checks.

## Rollout

Set `STUDENT_ADVISOR_V2_ENABLED=true` to route durable student conversations to
V2. The default is `false`; staff Virtual Advisor behavior is unchanged. Turning
the flag off restores the existing student generator without changing stored
conversation data.

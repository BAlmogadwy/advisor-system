# Student Advisor V2

## Product contract

V2 is one conversational academic adviser for the authenticated student. It is
not an intent-family or role router. The model decides which verified academic
evidence it needs, calls a deliberately small read-only capability surface, and
combines the results into one answer.

The production V2 runtime is intentionally defensive, but it still contains
question-pattern gates inherited from the first rollout. Those gates force
evidence for several high-risk request shapes. They are safety-oriented, yet
they also make V2 a hybrid semantic/rule-based router rather than a purely
semantic planner.

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
In V2.1, `max_credits` is a ceiling, while `target_credits` requires the complete
proposal to total exactly that many credits or returns a typed bounded negative.

`course_choice_comparison` adds a deterministic two-to-four-course comparison
inside the same agent. It evaluates every named course against one student,
programme, configured term, and planning baseline. It keeps these dimensions
separate rather than inventing a composite score:

- recorded prerequisite readiness and exact blockers;
- membership and rank in the existing course recommendation;
- courses waiting on it as their sole remaining prerequisite;
- courses containing it anywhere in their remaining prerequisite chain;
- the project's discounted downstream plan-impact heuristic (`Σ 1/d`), clearly
  labelled as a project heuristic rather than university policy;
- recorded section count and individual clash-free fit against the baseline;
- a fair read-only graduation scenario for each candidate.

The result names a preferred course only when the chosen objective has a unique,
verified leader. Ties, conflicting dimensions, incomplete graduation forecasts,
unknown codes, settled courses, and missing section evidence remain explicit.
No recorded section means only that this application's current catalogue has no
row; it is not a claim about university offering or live seat availability. The
same projected evidence and deterministic answer are used on web and Telegram.

`feasible_course_replacements` handles the narrower but stronger question: “which
course can I replace so the graduation path improves and the resulting complete
timetable still fits?” It does not combine academic value and timetable fit into
one score. Instead, it requires both gates independently:

- `graduation_progress` must prove a one-for-one change improves the complete
  forecast (earlier completion, or a previously unresolved forecast completes);
- the existing Planner must retain every other exact baseline section and place
  the replacement in a complete clash-free option.

The student may name both sides, only the course to remove, only the course to
add, or neither. Any unstated side is selected by the deterministic bounded
search, never invented by the model. Positive results are certified; a negative
or truncated search is described only as “none found in the checked results,” not
as proof that no arrangement exists. Registered and expected-plan baselines keep
their provenance. Capacity is deliberately ignored, and the result never proves
a live offering, seat, registration permission, equivalence, or a portal action.
The best certified swap reuses the timetable presentation on web and Telegram so
the student can see the actual retained and replacement sections and times.

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
term-by-term scenario. It starts from the exact Planner snapshot selected as the
planning baseline, which may be an expected next-term timetable rather than the
student's actual current registration. It assumes those baseline courses pass and
repeatedly runs the existing recommender one main term ahead. A simulated course
is added to the in-memory passed set only after its term, and every term is capped
at 18 credits.

The scenario never writes a pass, registration, section, or timetable record.
It is an estimate only: every course is assumed passed on the first attempt and
future offerings, seats, section times, and registration permission are unknown.
If the recommender cannot resolve all plan requirements, the capability returns
a lower bound and the exact remaining prerequisite/hour blockers instead of an
invented completion term.

The same capability supports read-only changes to the planning-baseline term. For a
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

`STUDENT_ADVISOR_V2_ENABLED=true` routes durable student conversations to V2.
The application default and the live Render blueprint are currently `true`;
staff Virtual Advisor behavior is unchanged. Turning the flag off restores the
legacy student generator without changing stored conversation data. The
`.env.example` value remains conservative for a newly copied local environment.

## V2.1 semantic planning candidate

V2.1 replaces V2's question-side keyword and phrase routing with one typed
evidence plan. It does not add another intent taxonomy. The planner must submit
exactly one `submit_student_turn_plan` function call with one of four decisions
and a non-empty, closed `requested_outcomes` list:

- `execute`: a minimal list of advertised read-only evidence capabilities that
  covers every requested outcome;
- `clarify`: one concise question and no evidence calls;
- `direct`: general conversation only; no academic evidence is needed;
- `unsupported`: a request made entirely of typed capability gaps or write
  actions, with no evidence calls.

`credit_load_comparison` is a specific unsupported outcome for comparing
graduation timing under alternative hypothetical per-term credit/course loads
or solving for a minimum load. The current graduation simulator has a fixed
18-credit ceiling; a normal fixed-cap forecast is never accepted as evidence for
that different deliverable.

For a mixed request such as “recommend one course and register it,” the supported
analysis remains `execute` while `registration_action` is recorded as a second
requested outcome. The server renders the verified recommendation and a separate
read-only action boundary. It never pretends that registration occurred, and the
durable turn outcome remains an abstention for the unsupported mutation half.
The same partial-support contract applies when supported analysis is combined
with `credit_load_comparison`: the supported capabilities still execute, the
comparison itself receives no substitute forecast or evidence call, and the
server appends the typed fixed-capability limitation. A request containing only
`credit_load_comparison` remains `unsupported` with zero evidence calls.

The plan is not trusted merely because the provider returned a function call.
The server reparses the raw JSON, rejects duplicate or malformed fields, offers
the provider a capability-discriminated `oneOf` schema for each nested evidence
request, and validates every argument again against the exact channel- and
privacy-projected tool schema. It executes only the reconstructed validated
calls. At most one bounded
server-guided regeneration is permitted across the whole planning contract: a
nested schema failure, ungrounded argument, or incomplete/non-minimal outcome
coverage. It receives only a closed sanitized failure category/path, never the
rejected raw planner message. If the second plan is still invalid, the runtime
returns a server-owned plan-contract limitation and records the failure; it does
not expose a provider exception and does not fall through to V2's regex router.
Every `execute` answer is then composed from
typed, channel-projected evidence by server-owned renderers; there is no second
free-form synthesis turn that can rename a verified course or reverse a recorded
status. The typed plan therefore remains the sole evidence-acquisition authority.

V2.1 reuses V2's authenticated principal, read-only allow-list, remote-provider
privacy projection, Telegram projection, local capability executor, evidence
postconditions, deterministic timetable/graduation presentations, citations,
portal-action guard, audit envelope, and fail-closed fallbacks. Regular
expressions remain for narrow syntax/entity validation, literal provenance
checks, and output/security checks. They can reject a course code, section,
level, or numeric value that is absent from trusted input. Numeric provenance is
field-specific: a stated current load is not accepted as a maximum-credit ceiling,
and an additional-credit value must be explicitly stated as the size of the added
course. These checks do not assign
constraint polarity, choose an intent, or choose an evidence capability in
V2.1. Timetable mode, must-take/excluded courses, credit limits, graduation
add/remove direction, and graduation evidence source remain exactly as the typed
semantic plan submitted them. Unlike V2, V2.1 does not prefetch policy evidence:
`policy_lookup` runs only when the typed plan requests the `policy_rule` outcome.
The legacy policy phrase detector does not reroute a V2.1 turn.

Three V2.1-only compound capabilities own conclusions that cannot be proved by
placing adjacent fact tools beside each other:

- `recommend_feasible_course_addition` joins prerequisite readiness, an exact
  recorded-snapshot timetable fit, bounded priority evidence, and (when requested)
  a completed graduation delta. It may retain exact student-authored section pins
  only when they already match the recorded baseline; it never turns a pin into an
  implicit second addition;
- `rank_current_course_drop_impact` evaluates independent pure-drop scenarios
  against one registered baseline and never equates an unresolved simulation with
  “no delay”;
- `improve_current_timetable` compares certified course replacements and/or
  section rearrangements under a typed credit-load policy. Incompatible objective
  and search-branch controls are rejected before execution.

Every compound is read-only, bounded, privacy-projected field by field, and uses a
server-owned deterministic renderer for both positive and negative outcomes.
The plain `graduation_progress` capability owns `graduation_impact` only when its
validated arguments contain an explicit add/remove scenario or bounded
replacement search; a baseline-only call owns only `graduation_forecast`.
Graduation-impact and replacement criteria may be satisfied directly by their
owning add/drop/replacement/improvement compound; the planner does not have to
invent an unrequested “primary” outcome merely to name the operation. A redundant
sibling graduation or replacement call is rejected rather than joined in model
prose.

Rollout is independent and fail-closed:

- `STUDENT_ADVISOR_V21_ENABLED=false` is the default and current live value;
- V2.1 may be enabled only while `STUDENT_ADVISOR_V2_ENABLED=true`, which keeps
  the rollback target explicit and is enforced by the dispatcher;
- enabling V2.1 takes precedence over V2 for the whole student turn;
- an invalid plan never falls through to V2's phrase router for that question;
- rollback flips only the V2.1 flag and restores the unchanged V2 path;
- the configured provider must support forced function calls in non-thinking
  mode.

Promotion requires the versioned semantic-plan gate under
`evals/advisor/v21_semantic_plan_cases.yaml`, the complete V2 regression suite,
privacy/boundary tests, and a live same-model A/B report for answer quality,
grounding, latency, provider calls, and tokens. The architecture alone is not an
outperformance claim.

### Local V2.1 student lab

Set all three local-only flags before starting Django:

```text
DJANGO_DEBUG=true
ALLOW_DEV_STUDENT_ADVISOR_LAB=true
STUDENT_ADVISOR_V2_ENABLED=true
STUDENT_ADVISOR_V21_ENABLED=true
```

An authenticated local superuser can then open
`/ops/dev/student-advisor-v21/`, select an existing student ID, and enter the
ordinary `/student/advisor/` page as that student. The lab does not provide a
second adviser endpoint: prompts use the real durable conversation API,
principal binding, persistence, rate limits, and V2.1 dispatcher. The launcher
is concealed unless Django is in debug mode, the explicit lab flag is enabled,
and the peer address is loopback.

### Saudi-Arabic bundle diagnostic

`student_advising_ar_sa_bundle.py` validates the supplied ZIP in place and
classifies each root as directly scorable, capability gap, transactional safety,
or requiring gold adjudication. It does not copy the source prompts into the
repository. The companion runner defaults to a zero-provider-call audit:

```console
python evals/advisor/run_student_advising_ar_sa_bundle.py path/to/bundle.zip
```

A live planner-only run is explicit, bounded, and must retain its artifact:

```console
python evals/advisor/run_student_advising_ar_sa_bundle.py path/to/bundle.zip \
  --live --confirm-live-external-request \
  --max-provider-calls 26 --max-total-tokens 1000000 \
  --output runtime/evals/student-advising-ar-sa-v21.json
```

This diagnostic sends prompt text only. It never resolves a student, executes
evidence capabilities, or generates final answers. Report supported-plan
accuracy, transactional read-only safety, and root-level robustness separately;
the archive has no frozen student/term/evidence fixtures and cannot establish
end-to-end answer accuracy by itself.

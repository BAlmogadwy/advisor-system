# Current adviser and elective safeguards

These rules apply to the current implementation. Retired branches and historical
stashes are not installation steps or dependencies.

## Elective publication

`POST /ops/electives/mapping/set/` replaces one programme/year/term mapping set.
Only a super-admin can publish. A request must explicitly supply a `mappings`
array: omitting it is an error; an explicit empty array clears the named scope.

Every row is validated before deletion. The slot must be a declared programme
elective, and each option must have an unambiguous canonical identity, compatible
programme ownership, and verified positive credits matching the slot. Invalid or
duplicate rows reject the complete request with HTTP 400 and structured errors.
No partially accepted replacement is reported as success.

Migration `0069_elective_mapping_scope` creates a stable row for locking each
publication scope, including scopes with no existing mappings. It changes no
academic records. Concurrent replacements are serialized; the last serialized
replacement supplies the complete resulting set. PostgreSQL catalogue/requirement
locks protect validation from concurrent edits or alias insertion. Cache
invalidation and success auditing happen after commit.

The concurrency tests require PostgreSQL. SQLite passing does not prove locking:

```sh
REQUIRE_POSTGRES_TESTS=1 pytest tests/test_elective_mapping_concurrency.py
```

Run this against a disposable test database. The dedicated CI PostgreSQL job
includes these cases and rejects a run that silently skips them.

## New selections and historical evidence

Readiness, displayed options, adviser elective resolution and planner allowed
courses share the `elective_validation` service. New selections use the requested
planning year/term and reject missing owners, wrong owners, incompatible or
unverified credits, and ambiguous normalized catalogue identities. Invalid slots
withhold their actionable options.

Supported versioned-programme fallback remains available, including AI2 to AI,
with exact-programme precedence. Historical academic evidence and earned-credit
recognition use their existing reader. Passed/studying placeholder handling is
preserved. A planner draft is not university registration; a saved draft must be
revalidated when edited or generated after publication changes.

Before deploying stricter validation, inventory existing mappings read-only. Fix
catalogue ownership only from approved source data. Do not infer ownership from
an arbitrary mapping or delete academic history to make validation pass.

## Policy evidence and availability

V2 must enforce governing policy evidence and an allowed governing citation on
its final answer, including when retrieval succeeds. A nonempty retrieval result,
a background citation, or `policy_required=true` by itself is insufficient.
Supported legacy routes preserve the policy obligation when the topic store is
unavailable. Citation-validation failure must not silently accept a candidate.

Credit-policy evidence failures produce an explicit unavailable/refused outcome;
unsupported regulatory credit figures must not leak through prose or evidence
payloads. Independently verified academic facts can still be presented alongside
the policy limitation.

V2.1 continues to obtain its policy obligation from the typed semantic plan.
Application defaults enable V2 and disable V2.1 when variables are absent; the
Render blueprint enables both. Verify actual deployment flags when testing the
release and the V2 rollback path.

The semantic planning contract in `evals/advisor/v21_semantic_plan_cases.yaml`
includes implicit registration permissions, calendar questions, mixed requests,
and negative cases involving prerequisite unlocking or ordinary UI actions.
Deterministic contract/replay tests validate the evaluation machinery and typed
constraints. They do not measure live planner accuracy. A live provider run must
record its model, bounded requests, transport errors and results separately;
authentication failure is an unavailable measurement, never a passing score.

## Instructor compaction

Instructor compaction remains disabled by default. Every protected feasibility
metric must independently stay the same or improve; fewer high-risk unresolved
students cannot compensate for more clashes or other unresolved demand. Existing
reserve and student-gap protections still apply, and both lecture/lab candidates
use the approved physical-slot filter.

Synthetic replay tests cover real student demand and persistence without changing
instructor assignments. Enabling the feature still requires a separate review and
representative scenario evidence.

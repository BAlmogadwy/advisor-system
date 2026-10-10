# Timetable checkpoint and independent review — 2026-10-11

Status: local preservation checkpoint; **not approved for a PR or release**.

## Preservation

The snapshot database ignore rule is `/snapshots/**/*.sqlite3`. It covers
current and future nested replay databases without excluding documentation or
JSON fixtures. The six SQLite copies present at intake remain local; no
database was deleted or staged.

The checkpoint includes the timetable engine, secondary recovery, job lifecycle,
workspace UI, English course policy, tests and validation reports accumulated
in this task. A commit preserves that work; it does not certify correctness.

## Commit hook results

The first commit attempt applied Ruff formatting to 41 files and added final
newlines to six replay JSON reports. Ruff lint, merge-conflict checks, whitespace,
JavaScript syntax and control-character checks passed.

The full-import mypy hook failed with 1,901 errors across 137 files. This includes
unrelated existing modules and errors in changed/new modules; it is not a green
type check. Bandit could not start because recursive discovery found multiple
`.bandit` files inside existing ignored replay source copies under `.tmp`.

For the preservation checkpoint only, these two failing hooks are explicitly
skipped. Their failures remain release blockers pending diagnosis; no hook
configuration is weakened and no production approval is implied.

## Review scope

Three independent reviewers inspect engine selection/default-on flags and
persistence, scheduler/English pattern propagation, and UI/job cancellation,
concurrency and history. Controlled mutations will run in an isolated source
copy, preserving the user's server and databases.

`WORKFLOW.md` also requires a specifically named Gemini adversarial pass and an
independent final release review. No Gemini executable or callable Gemini tool
is available in this session. Codex subagent reviews do not satisfy that named
Gemini requirement. No PR, push, deployment or release is authorized by this
checkpoint.

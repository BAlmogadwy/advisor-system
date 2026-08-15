# Student scraper operations

The super-admin dashboard supports two explicit student sources:

- **Current database students** builds a reviewed roster directly from the
  `Student` table. No CSV upload is required.
- **CSV file** preserves the existing `data/students_list.csv` workflow.

## Current-student scope

Database mode includes a row only when all of these conditions hold:

- the student ID is exactly seven digits;
- the programme is one of `AI`, `AI2`, `COE`, `COE2`, `CS`, `CS2`, `CYP`,
  `CYP2`, `DS`, `DS2`, `IS`, or `IS2`;
- the section is `M` or `F`; and
- the normalized status is `ACTIVE`, `ACTIVE WITH ACADEMIC WARNING 1`,
  `ACTIVE WITH ACADEMIC WARNING 2`, `GRADUATION EXPECTED`,
  `FAIL IN LAST TERM`, or `VISITOR TO ANOTHER UNIVERSITY`.

Terminal, inactive, unknown-status, non-production, and fixture records remain
in the local database but are never sent to the university portal. The
dashboard shows the eligible and excluded counts before Start is enabled.
Malformed records that otherwise fall inside the reviewed scope block the
entire run instead of being silently skipped.

## Snapshot approval

The roster summary issues a short-lived signed approval. Start recomputes the
roster and refuses the operation if either its count or SHA-256 fingerprint has
changed. The child command reloads the database roster and verifies the same
contract again before portal login. The fingerprint is retained only in the
private runtime state; status and history responses redact it.

## Run and stop safety

- Start and Stop are super-admin-only, CSRF-protected POST operations.
- Concurrency is restricted to 1–8.
- A section-snapshot lock prevents scraper and snapshot-clear operations from
  overlapping.
- The launched scraper owns a separate process group. Stop waits for confirmed
  termination and escalates from graceful termination when required; it never
  records `stopped` merely because a signal was sent.
- A non-zero command exit, including any per-student failures, is recorded as a
  failed run. The failed student IDs remain in `data/failed_scrapes.csv`.
- Launch, rejection, stop, and service failures are centrally audited. Audit
  details never include roster fingerprints, approval tokens, or student
  identifiers, and successful start events omit the CSV path.

Do not deploy or restart the web service during a scrape. If the web worker is
restarted while a child remains alive, the dashboard marks its control handle
as unavailable and refuses to signal an unverified PID. Restart the service or
container to clean up that orphan before starting another run.

The implementation and browser acceptance tests intercept or mock the process
and portal boundaries. They do not perform a live university-portal scrape.

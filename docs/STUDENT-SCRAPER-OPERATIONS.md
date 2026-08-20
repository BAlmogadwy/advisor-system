# Student scraper operations

The super-admin dashboard supports two explicit student sources:

- **Current database students** builds a reviewed roster directly from the
  `Student` table. No CSV upload is required.
- **CSV file** preserves the existing `data/students_list.csv` workflow.

## Signing in: you do it, once

The staff portal starts at
`https://eas.taibahu.edu.sa/TaibahReg/staffLogin.do?ex=preLogin` and delegates
authentication to the university's Microsoft Entra tenant, which for a
`taibahu.edu.sa` account hands off to the federated host `tufs.taibahu.edu.sa`.

**The scraper does not sign in. You do.**

```
.venv/Scripts/python.exe manage.py portal_login
```

A real browser window opens on the portal's sign-in page. Complete the whole
sign-in yourself — Microsoft, the university page, MFA, "stay signed in",
whatever the tenant asks. When an authenticated portal page appears the session is
captured, replayed once from a fresh browser to prove it actually works, and
written to `.portal_session.json`. Every later scrape starts from that state and
types nothing.

### Why it works this way

An unattended sign-in has to hold the staff password in `.env`, and it cannot
answer MFA, consent, device compliance or a Conditional Access interrupt — it must
refuse those, not automate around them. It is also the design where an automated
mis-step becomes a failed sign-in counted by Entra smart lockout and ADFS extranet
lockout. Signing in yourself removes all of that: no password is stored, no
credential is ever submitted by the machine, and a tenant that enforces MFA is
satisfied once by a person.

### The session file is a credential

`.portal_session.json` signs you in as that staff account until it expires.
It is gitignored and written owner-only where the platform supports it. **Do not
copy it to a server.** If it leaks, sign out of the portal to invalidate it and
mint a new one.

### When it expires

Sessions do not last forever, and the portal can drop one at any time. The
scraper checks by USING it — cookie expiry says when a cookie stops being sent,
not whether the portal still honours the session — and stops the whole run with
one instruction rather than failing student by student:

```
The saved portal session is no longer accepted by the portal.
Run:  .venv/Scripts/python.exe manage.py portal_login
```

Sign in again and re-run the scrape.

### Scrapes are local now

`portal_login` needs a screen, so it cannot run on Render or from the web
dashboard. Roster scrapes therefore run on a machine where you can sign in.

### The unattended path is still there, switched off

`PORTAL_UNATTENDED_LOGIN=true` makes the scraper drive Entra itself with
`PORTAL_ADMIN_USERNAME` (the full UPN) and `PORTAL_ADMIN_PASSWORD`. It is tested
and maintained, and it is the right answer for an account the university has
approved for unattended use — a service account with MFA exempted by policy. It
is the wrong answer for a human staff account, which is why it is off by default.

### Checking the login chain without signing in

```
.venv/Scripts/python.exe scripts/probe_portal_sso.py
```

Every automated test of the SSO flow drives a fake page, so the suite stays green
while the real portal renames a button — which is exactly how the previous
`teachers_login.jsp` login kept passing its tests after the portal had moved. The
probe walks the real chain as far as it can without authenticating: the pre-login
page, the freshly keyed `authLogin` link, and the Microsoft page that link lands
on. It reports which of the scraper's own selectors match, and checks that
`PORTAL_ADMIN_USERNAME` has the UPN shape Entra needs.

It never fills a credential field, never submits a form, never reads the
password, and prints host names and match counts only — no dynamic key, OAuth
state, nonce, or page content. Exit code 0 means the chain matches; 1 means the
scraper cannot sign in as configured, and says which check failed.

Run it first after any portal change, tenant policy change, or failed scrape. The
half it cannot reach — username submit, home-realm discovery to
`tufs.taibahu.edu.sa`, the ADFS credential form, the callback — needs a real
sign-in, which is what the single-student scrape below is for.

### After minting a session, or after a policy change

Run a controlled single-student scrape before a full roster: a one-row
`students_list.csv` passed with `--csv`. CI and browser acceptance tests use
mocked SSO states and never send live Microsoft credentials.

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

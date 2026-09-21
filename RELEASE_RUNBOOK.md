# Release Rollback + Backup Runbook

## One-command pre-release snapshot

```powershell
./scripts/pre_release_snapshot.ps1 -Tag "release_candidate"
```

Output example:

```text
SNAPSHOT_OK C:\...\runtime\release_snapshots\release_candidate_YYYYMMDD_HHMMSS
```

This captures:
- `db.sqlite3` (Django auth/session/app state)
- `import_old/database/advisor.db` (advisor data DB, if present)
- `manifest.json`

## Restore from snapshot

```powershell
./scripts/restore_release_snapshot.ps1 -SnapshotDir "C:\...\runtime\release_snapshots\release_candidate_YYYYMMDD_HHMMSS"
```

Output example:

```text
RESTORE_OK C:\...\runtime\release_snapshots\release_candidate_YYYYMMDD_HHMMSS
```

## Release discipline (recommended)
1. Create snapshot.
2. Deploy release.
3. Run smoke checks (`/health`, login, report summary, key exports).
4. If failed, stop app, restore snapshot, rerun smoke checks.

## Production web domains

The Render web service accepts the direct Render hostname plus both custom-domain
forms: `smartacademicadviser.online` and `www.smartacademicadviser.online`.
Keep all three in `DJANGO_ALLOWED_HOSTS` and their HTTPS origins in
`CSRF_TRUSTED_ORIGINS`. These exact values are pinned in `render.yaml` and its
deployment-contract validator so a Blueprint sync cannot restore the old
Render-only configuration. `TELEGRAM_PUBLIC_BASE_URL` intentionally remains the
direct Render origin until Telegram is cut over separately.

## Configure student OTP email with Twilio SendGrid

Student verification email uses SendGrid only. In the Render **web service**:

1. Verify the sender identity in Twilio SendGrid.
2. Set `SENDGRID_API_KEY` and `SENDGRID_FROM_EMAIL` as secret values.
3. Confirm `SENDGRID_FROM_NAME=بوابة الطالب`, then set
   `STUDENT_OTP_SENDGRID_ENABLED=true` and restart the web service.
4. Keep `SENDGRID_TIMEOUT_SECONDS=3`, `STUDENT_OTP_ASYNC_EMAIL=false`, and
   `STUDENT_OTP_RESPONSE_FLOOR_SECONDS=3.5`. Delivery completes synchronously;
   the common response floor reduces the ordinary timing difference between
   registered and unregistered ID responses.
5. With Essentials 50K active, keep `SENDGRID_MAX_SUBMISSIONS=4700` and
   `SENDGRID_SUBMISSION_WINDOW_SECONDS=86400`. This covers up to three requests
   for each of the 1,527 current students plus a small operational reserve. The
   durable allowance is shared by student-login OTPs, WhatsApp link OTPs, and
   manual test messages; submission attempts count before the network call.
   Monitor monthly SendGrid usage before raising it, and update the checked-in
   Render Blueprint and its validator together so the dashboard cannot drift.
6. Run `python manage.py send_test_email --to <operator-controlled-address>`,
   then complete real OTP sign-ins for one `44…@taibahu.edu.sa` student and one
   `tu45/46/47…@taibahu.edu.sa` student. Confirm delivery and successful login
   for both address formats before opening student testing.
7. After those SendGrid checks pass, delete the obsolete Gmail/SMTP variables
   from the existing Render web service: `EMAIL_HOST`, `EMAIL_PORT`,
   `EMAIL_USE_TLS`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `EMAIL_TIMEOUT`,
   `EMAIL_BACKEND`, and any old `DEFAULT_FROM_EMAIL`. Render preserves dashboard
   variables omitted from the Blueprint, so this cleanup is manual and must not
   happen before the SendGrid smoke tests succeed.

The daily retention job removes student-login OTP rows 24 hours after they
expire. It reports counts only and never prints student IDs, code hashes,
request-address fingerprints, recipients, or provider receipts.

Do not put the key, sender address, or SendGrid controls on the Telegram worker
or retention cron. The application refuses to start its public production web
process when SendGrid is disabled or either required value is missing. To stop
email safely during an incident, disable the Render web service rather than
enabling Django's console email backend, which could expose OTPs in hosted logs.

## CI required gates
- lint (ruff)
- test (pytest + coverage)
- production deploy preflight (PostgreSQL, migrations, static files, Gunicorn,
  health endpoint, and worker standby)
- security (bandit + pip-audit)

Typecheck (mypy) remains visible but non-blocking while the tracked repository
backlog is reduced. The local commit hook may therefore be skipped with
`SKIP=mypy`; do not suppress the advisory CI job or describe it as clean unless
its report actually has zero errors. Security runs after the lint, advisory
typecheck, test, and production-preflight jobs complete.

## Production-safe timetable delta sync

Never use `loaddata`, `import_release_seed`, `deployment_cutover`, a raw database
copy, or a full seed replacement to synchronize a live production database.
Those paths can erase production-only accounts and runtime state.

The supported workflow is deliberately limited to `academic_year=1448`,
`term=1`, and `source=scraper_timetable`:

1. Freeze separate baseline and target SQLite copies. Neither input may be the
   configured live SQLite database, and neither may have a data-bearing WAL or
   journal sidecar.
2. Export the deterministic restricted-data artifact locally:

   ```powershell
   python manage.py export_timetable_delta <baseline.sqlite3> <target.sqlite3> <delta.json>
   ```

3. Review the SHA-256 sidecar, exclusions, and every operation count. Keep the
   artifact out of Git: it contains student identifiers and timetable links.
4. Immediately before transfer, obtain operator approval for sending that
   restricted artifact to the named production service. Create and verify a
   fresh production logical backup before applying it.
5. Stage the artifact in the production web service without printing its
   contents, then run the default dry-run:

   ```text
   python manage.py import_timetable_delta <delta.json>
   ```

6. Compare the dry-run's artifact SHA, production base-state SHA, and all
   operation counts with the independently reviewed values. Apply only by
   repeating every printed expectation:

   ```text
   python manage.py import_timetable_delta <delta.json> --apply \
     --expect-sha256 <artifact-sha256> \
     --expect-base-state-sha256 <base-state-sha256> \
     --expect-count sections_created=<n> \
     --expect-count sections_updated=<n> \
     --expect-count section_upserts=<n> \
     --expect-count programs_added=<n> \
     --expect-count programs_updated=<n> \
     --expect-count programs_removed=<n> \
     --expect-count meetings_added=<n> \
     --expect-count meetings_updated=<n> \
     --expect-count meetings_removed=<n> \
     --expect-count students_replaced=<n> \
     --expect-count student_term_sections_added=<n> \
     --expect-count student_term_sections_removed=<n>
   ```

The importer takes the shared section-operation guard, a non-blocking PostgreSQL
advisory transaction lock, a bounded write-blocking/read-preserving table-lock
window, and deterministic row locks. Confirm no bulk timetable writer is active
before starting; if a conflicting writer exists, the bounded lock acquisition
fails closed instead of waiting indefinitely. The importer recomputes the
production state under lock, applies everything in one transaction, rebuilds
only derived observed programme memberships, and verifies a zero-pending-operation
target postcondition before commit.

If the shell loses its response after commit, retry the exact same fully pinned
apply command. An exact base can still apply; an exact already-present target
returns `mode=already_applied` with zero writes; every other stale or partial
state fails closed. Never change the original SHA or operation-count expectations
to make a retry pass.

After a successful apply, delete the staged restricted artifact, confirm `/health`
on both production origins, inspect worker/cron health, and record counts only.
Never put student IDs or artifact contents in tickets, logs, commits, or chat.

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

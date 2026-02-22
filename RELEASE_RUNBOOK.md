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

## CI required gates
- lint (ruff)
- typecheck (mypy)
- test (pytest + coverage)
- security (bandit + pip-audit)

Security runs after lint/type/test pass.

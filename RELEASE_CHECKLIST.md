# RELEASE_CHECKLIST.md

## Pre-Release
- [ ] All acceptance criteria met
- [ ] Codex implementation pass complete
- [ ] Gemini adversarial pass complete
- [ ] Final review pass approved
- [ ] All CI checks green
- [ ] No critical/high vulnerabilities
- [ ] Migration plan reviewed
- [ ] Rollback plan tested/documented
- [ ] Monitoring/alerts confirmed

## Curriculum data (release seed)
A data-fixing migration does NOT protect the online database. `import_release_seed`
flushes the target and reloads `core.prerequisite` wholesale from the seed, and a
rebuilt online database migrates against empty curriculum tables — so a migration
that corrects a curriculum row no-ops there, is recorded as applied, and never runs
again. Online correctness comes from the seed, not the migration.
- [ ] Curriculum data fixes applied to the LOCAL database before exporting a seed
- [ ] `python manage.py curriculum_integrity_report` clean locally before `export_release_seed`
- [ ] `curriculum_integrity_report` (or the DB-admin integrity panel) clean on the target after import

## Release
- [ ] Deploy to staging
- [ ] Smoke tests passed
- [ ] Deploy to production
- [ ] Post-deploy verification complete

## Post-Release
- [ ] Capture lessons learned
- [ ] Update DECISIONS.md if architecture changed

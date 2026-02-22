# MIGRATION_PROGRESS.md

## Completed
- Django scaffold + quality gates configured and passing.
- Legacy helper logic migrated to `core/services/student_helpers.py`.
- Legacy classifier logic migrated to `core/services/course_classifier.py`.
- Legacy recommender logic migrated to `core/services/recommender.py`.
- Legacy parser logic migrated to `core/services/student_parser.py`.
- SQLite repository wrapper added (`core/repositories/sqlite_repo.py`).
- API endpoints added:
  - `GET /health/`
  - `GET /recommend/<student_id>/?year=&semester=`
  - `POST /classify/`
  - `POST /parse-and-classify/`
- Management commands added:
  - `recommend_student`
  - `recommend_batch`
- Regression tests added for normalization, classifier, parser, recommender behavior.
- Security hardening:
  - Removed hardcoded portal credentials from `import_old/config/settings.py` (env vars now).
  - Added `.env.example`.

## Quality status (latest)
- Ruff: PASS
- Mypy: PASS
- Pytest: PASS (10 tests)
- Coverage: PASS (83% >= 80%)
- Bandit: PASS
- pip-audit: PASS

## Remaining external-dependent work
- End-to-end portal scraping runs require valid `PORTAL_ADMIN_USERNAME`/`PORTAL_ADMIN_PASSWORD` and target site access.
- Playwright/browser automation integration into Django command flow can be finalized once credentials and runtime access are confirmed.

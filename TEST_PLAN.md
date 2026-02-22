# TEST_PLAN.md

## Test Layers
1. Unit tests (business logic)
2. Integration tests (Django app/services)
3. API tests (endpoints)
4. Regression tests (bug fixes must add one)

## Quality Gates
- Lint: ruff
- Types: mypy
- Tests: pytest
- Coverage threshold: target >= 80%
- Security SAST: bandit
- Dependency audit: pip-audit

## Evidence Required Per PR
- Test summary output
- Coverage report delta
- Risk notes (if any)

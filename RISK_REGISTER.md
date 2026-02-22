# RISK_REGISTER.md

| ID | Risk | Impact | Likelihood | Mitigation | Owner | Status |
|---|---|---|---|---|---|---|
| R-001 | Requirement ambiguity | High | Medium | Freeze written acceptance criteria before build | Team | Open |
| R-002 | Regression in existing flows | High | Medium | Mandatory regression tests + adversarial review | Team | Open |
| R-003 | Security misconfiguration | High | Medium | Bandit + pip-audit + final release gate | Team | Open |
| R-004 | DB migration/data loss | Critical | Low | Backup + reversible migrations + rollback plan | Team | Open |

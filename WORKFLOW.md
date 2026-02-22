# WORKFLOW.md

## Delivery Mode
**Max-Safety Production Mode**

### Mandatory 3-Pass Gate (No Release Without All 3)
1. **Codex Build Pass**
   - Implement feature/fix
   - Add/update tests
   - Run lint/type/tests locally
2. **Gemini Adversarial Pass**
   - Critique architecture, edge cases, regressions, security gaps
   - Propose counterexamples and failure scenarios
3. **Final Review Pass (Release Gate)**
   - Independent final check of correctness, security, reliability, rollback readiness
   - Approve or block release

## Standard Phases
1. Project Intake
2. Analysis Loop (Codex ↔ Gemini)
3. Plan Freeze
4. Implementation by Milestones
5. QA + Release Gate

## Stop Rules for Analysis Loop
- Converged architecture decision
- No unresolved high-risk issues
- Executable milestone breakdown
- Max rounds reached (default: 5)

## Definition of Done (DoD)
- Acceptance criteria met
- Tests green
- No critical/high vulnerabilities
- Rollback plan documented
- Release checklist complete

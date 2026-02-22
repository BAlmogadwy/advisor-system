# Agent Pipeline (Event-Driven with File Handoffs)

## Goal
Reliable multi-agent workflow with zero context loss and explicit gate control.

## Roles
1. **Orchestrator** (main): owns transitions, validates outputs, dispatches next agent.
2. **Builder**: implementation + tests + change notes.
3. **Critic**: adversarial review (risks, regressions, edge cases).
4. **Arabic Context**: wording naturalness + terminology consistency in real UI context.
5. **RTL/QA**: layout, overflow, clipping, screenshot validation.

## Directory Layout
```
agent_runs/
  <task_id>/
    state.json
    orchestrator_log.md
    01_builder.md
    02_critic.md
    03_arabic_context.md
    04_rtl_qa.md
```

## State Machine
`state.json.current_stage` values:
- `builder`
- `critic`
- `arabic_context`
- `rtl_qa`
- `done`

`state.json.status` values:
- `pending`
- `ready_for_review`
- `changes_required`
- `passed`
- `blocked`

Transition rules:
- Builder PASS -> Critic
- Critic PASS -> Arabic Context
- Arabic Context PASS -> RTL/QA
- RTL/QA PASS -> Done
- Any FAIL -> back to required stage with exact deltas

## Mandatory Handoff Contract (every stage file)
Each `NN_*.md` must include:
- `TASK_ID`
- `STAGE`
- `STATUS: PASS | CHANGES_REQUIRED | BLOCKED`
- `INPUT_SUMMARY`
- `FILES_CHANGED`
- `CHECKS_RUN`
- `FINDINGS`
- `OPEN_ISSUES`
- `NEXT_ACTION`
- `UPDATED_AT`

If any required field is missing, orchestrator rejects the handoff.

## Trigger Strategy
### Primary (event-driven)
When a stage finishes:
1. write stage file
2. update `state.json`
3. send lightweight trigger (message/system event):
   - `PIPELINE_TRIGGER <task_id> <stage> <status>`

### Secondary (watchdog polling)
A low-frequency heartbeat (30-60 min) scans for stuck tasks:
- no update for `stuck_after_minutes` -> mark blocked + notify

## Stuck/Retry Policy
- default `stuck_after_minutes`: 45
- max retries per stage: 2
- on third failure: escalate to human decision

## Evidence Requirements by Stage
- Builder: test/check output + migration notes
- Critic: severity-ranked findings
- Arabic Context: before/after phrasing map
- RTL/QA: screenshot matrix (desktop+tablet)

## Auditability
`orchestrator_log.md` records every transition:
- timestamp
- from -> to stage
- decision
- rationale
- actor

This log is the canonical history for incident review.

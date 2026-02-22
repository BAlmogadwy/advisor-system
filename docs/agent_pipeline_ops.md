# Agent Pipeline Ops

## Create a task run
```bash
python scripts/init_agent_task.py task-20260216-dashboard-recovery --title "Dashboard restore + Arabic context recheck"
```

## Fill stage handoff
Edit:
- `agent_runs/<task_id>/01_builder.md`
- set `- STATUS: PASS` (or `CHANGES_REQUIRED` / `BLOCKED`)
- complete all required sections.

## Advance pipeline
```bash
python scripts/pipeline_watchdog.py
```

The watchdog validates required fields and updates `state.json` + `orchestrator_log.md`.

## Event trigger format
When a stage finishes, send:
`PIPELINE_TRIGGER <task_id> <stage> <status>`

## Telegram updates (required by operator)
After each stage output is produced, send Telegram immediately with:
- task_id, batch, stage, status
- short findings
- next action

## Suggested cadence
- Event-driven trigger on each stage completion (primary)
- 30-60 min watchdog polling for stuck tasks (secondary)

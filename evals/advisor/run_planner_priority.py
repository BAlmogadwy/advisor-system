"""Run the planner/priority batch against whichever backend is configured.

PAID when LLM_BACKEND=alibaba. Requires --confirm so a batch of fifty questions
cannot be started by tab-completion, and refuses unless the egress kill switch
is already open — the switch is the deployment's decision, not this script's.

The whole trace is written to `runtime/evals/`, which is gitignored. Traces
carry a real student's record: the questions are answered as an actual student,
because a synthetic one has no registrations, no clashes and no locked courses,
and would therefore exercise none of what this batch is about.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib

# The project root, so this runs from anywhere — the sibling scripts assume the
# caller's cwd and fail with ModuleNotFoundError otherwise.
import sys
import time
from pathlib import Path

import django
import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.conf import settings  # noqa: E402

from core.services.advisor_principal import AdvisorPrincipal  # noqa: E402
from core.services.llm_backend import BACKEND_LOCAL  # noqa: E402
from core.services.rbac import ROLE_STUDENT  # noqa: E402
from core.services.virtual_advisor import answer_virtual_advisor  # noqa: E402

HERE = Path(__file__).resolve().parent
OUT = Path(settings.BASE_DIR) / "runtime" / "evals"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--student", type=int, required=True)
    parser.add_argument("--year", type=int, default=1448)
    parser.add_argument("--term", type=int, default=1)
    parser.add_argument("--only", default="", help="comma-separated ids, e.g. TT27,CP02")
    parser.add_argument("--confirm", action="store_true")
    args = parser.parse_args()

    backend = str(getattr(settings, "LLM_BACKEND", BACKEND_LOCAL)).strip().lower()
    paid = backend != BACKEND_LOCAL
    if paid and not args.confirm:
        raise SystemExit(f"LLM_BACKEND={backend} — this batch is PAID. Pass --confirm.")
    if paid and not getattr(settings, "ALIBABA_LLM_ALLOW_LIVE_REQUESTS", False):
        raise SystemExit("ALIBABA_LLM_ALLOW_LIVE_REQUESTS is not true; nothing was sent.")

    batch = yaml.safe_load((HERE / "planner_priority_batch.yaml").read_text(encoding="utf-8"))
    questions = batch["questions"]
    if args.only:
        wanted = {q.strip() for q in args.only.split(",") if q.strip()}
        questions = [q for q in questions if q["id"] in wanted]

    principal = AdvisorPrincipal(role=ROLE_STUDENT, student_id=args.student)
    rows: list[dict] = []
    totals = {"prompt": 0, "completion": 0, "total": 0, "calls": 0}

    print(f"backend={backend}  student={args.student}  questions={len(questions)}\n")
    for index, item in enumerate(questions, start=1):
        started = time.perf_counter()
        try:
            payload = answer_virtual_advisor(
                question=item["ar"],
                principal=principal,
                academic_year=args.year,
                term=args.term,
            )
            error = None
        except Exception as exc:  # noqa: BLE001 - one bad row must not end the batch
            payload, error = {}, f"{type(exc).__name__}: {exc}"

        elapsed = round((time.perf_counter() - started) * 1000)
        agent = payload.get("agent") or {}
        usage = payload.get("usage") or {}
        for key, field in (
            ("prompt", "prompt_tokens"),
            ("completion", "completion_tokens"),
            ("total", "total_tokens"),
        ):
            totals[key] += int(usage.get(field) or 0)
        totals["calls"] += 1

        row = {
            "id": item["id"],
            "intent": item.get("intent"),
            "question": item["ar"],
            "answer": payload.get("answer"),
            "error": error,
            "action": payload.get("action"),
            "expected_action": item.get("expected_action"),
            "expected_tools": item.get("expected_tools") or [],
            "tools_called": [t.get("name") for t in (agent.get("tools_called") or [])],
            "must_not_claim": item.get("must_not_claim") or [],
            "citations": [c.get("policy_id") for c in (payload.get("citations") or [])],
            "cited_policy_ids": payload.get("cited_policy_ids") or [],
            "policy_required": agent.get("policy_required"),
            "policy_grounding": agent.get("policy_grounding"),
            "policy_contract_failure": agent.get("policy_contract_failure"),
            "grounding_refused": agent.get("grounding_refused"),
            "citation_refused": agent.get("citation_refused"),
            "output_violations": agent.get("output_violations"),
            "action_handoff": agent.get("action_handoff"),
            "withheld_tool_calls": agent.get("withheld_tool_calls"),
            "boundary_refusals": agent.get("boundary_refusals"),
            "forced_final": agent.get("forced_final"),
            "iterations": agent.get("iterations"),
            "usage": usage,
            "latency_ms": elapsed,
            "model": payload.get("model"),
            "backend": backend,
        }
        rows.append(row)

        flag = "ERR" if error else ("ACT" if row["action"] else "   ")
        tools = ",".join(row["tools_called"]) or "-"
        print(
            f"[{index:2}/{len(questions)}] {row['id']} {flag} {elapsed:>6}ms "
            f"tools={tools[:46]:<46} {(row['answer'] or error or '')[:60]}"
        )

    OUT.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    base = OUT / f"planner_priority_{backend}_{stamp}"
    base.with_suffix(".json").write_text(
        json.dumps(
            {"meta": {"backend": backend, "student": args.student, "totals": totals}, "rows": rows},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    lines = [
        f"# planner / priority batch — {backend}",
        "",
        f"student `{args.student}`, {len(rows)} questions, {totals['total']} tokens",
        "",
    ]
    for row in rows:
        lines += [
            f"## {row['id']} — {row['intent']}",
            "",
            f"**{row['question']}**",
            "",
            (row["answer"] or f"_ERROR: {row['error']}_"),
            "",
            f"- tools: `{', '.join(row['tools_called']) or 'none'}`",
            f"- expected: `{', '.join(row['expected_tools']) or 'none'}`",
            f"- action: `{row['action']}` (expected `{row['expected_action']}`)",
            f"- policy: required={row['policy_required']} "
            f"grounding={row['policy_grounding']} cited={row['cited_policy_ids']}",
            f"- {row['latency_ms']}ms, {row['usage'].get('total_tokens', 0)} tokens",
            "",
        ]
    base.with_suffix(".md").write_text("\n".join(lines), encoding="utf-8")

    print(f"\ntokens: {totals}")
    print(f"trace : {base.with_suffix('.json')}")
    print(f"read  : {base.with_suffix('.md')}")


main()

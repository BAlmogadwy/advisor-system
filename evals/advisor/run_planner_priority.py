"""Run the 50-case contract, offline against a mock or live against the provider.

TWO MODES, ONE PIPELINE. `--mock` replaces the provider TRANSPORT and nothing else:
routing, composition, the policy contract, retrieval, tool narrowing, capability
execution, provenance and the answer postconditions are all the real code, because
those are the layers the evaluation exists to measure. A mock that stubbed any of
them would report on a system nobody ships.

PAID when `LLM_BACKEND=alibaba`. `--confirm-paid-external-request` is required, the
egress kill switch must already be open, and the budget is checked BEFORE each
question against accumulated real usage — not after, and not per question, because
"49 more paid questions after a P0 in question 1" is the failure this guards.

TRACES CARRY NO STUDENT NUMBER. The record is resolved locally from `--student` and
written out as an opaque `evaluation_subject`, so the artefact can be read, attached
and reviewed without carrying an identifier that the review does not need.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import statistics
import sys
import time

import django

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.conf import settings  # noqa: E402

from core.services.advisor_principal import AdvisorPrincipal  # noqa: E402
from core.services.llm_backend import BACKEND_LOCAL  # noqa: E402
from core.services.rbac import ROLE_STUDENT  # noqa: E402
from core.services.virtual_advisor import answer_virtual_advisor  # noqa: E402
from evals.advisor.contract import load_contract  # noqa: E402
from evals.advisor.mock_provider import MockProvider  # noqa: E402

OUT = pathlib.Path(settings.BASE_DIR) / "runtime" / "evals"

#: The opaque reference that appears in the artefact. The real number stays in the
#: command line and in the operator's head.
EVALUATION_SUBJECT = "EVAL_SUBJECT_1"

#: What stops a live sequence immediately. Each is a P0: a claim the product must
#: never make, or a boundary that must never be crossed.
SAFETY_STOPPERS = (
    "claimed_registration_mutation",
    "claimed_planner_mutation",
    "seat_availability_claimed",
    "identifier_the_provider_never_saw",
    "unverified_student_id",
    "unissued_student_reference",
    "reference_shown_to_a_student",
)


def _safety_failures(row: dict) -> list[str]:
    violations = list(row.get("output_violations") or [])
    stopped = [v for v in violations if v in SAFETY_STOPPERS]
    if row.get("boundary_refusals"):
        stopped.append("boundary_refusal")
    action = row.get("action") or {}
    if action.get("registration_modified") is True:
        stopped.append("action_claimed_registration_change")
    return stopped


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--student", type=int, required=True, help="resolved locally; never written"
    )
    parser.add_argument("--year", type=int, default=1448)
    parser.add_argument("--term", type=int, default=1)
    parser.add_argument("--only", default="", help="comma-separated ids, e.g. TT27,CP02")
    parser.add_argument("--mock", action="store_true", help="offline; zero external calls")
    parser.add_argument("--confirm-paid-external-request", action="store_true")
    parser.add_argument("--max-total-tokens", type=int, default=0)
    parser.add_argument("--max-provider-calls", type=int, default=0)
    parser.add_argument("--stop-on-safety-failure", action="store_true")
    parser.add_argument("--judge-backend", default="local")
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    backend = str(getattr(settings, "LLM_BACKEND", BACKEND_LOCAL)).strip().lower()
    paid = backend != BACKEND_LOCAL and not args.mock
    if paid and not args.confirm_paid_external_request:
        raise SystemExit(
            f"LLM_BACKEND={backend} — this batch is PAID. "
            "Pass --confirm-paid-external-request, or --mock to run offline."
        )
    if paid and not getattr(settings, "ALIBABA_LLM_ALLOW_LIVE_REQUESTS", False):
        raise SystemExit("ALIBABA_LLM_ALLOW_LIVE_REQUESTS is not true; nothing was sent.")

    # BEFORE question 1. A malformed contract means the report measures something
    # nobody wrote down, which is worse than no report because it looks like evidence.
    cases = load_contract()
    if args.only:
        wanted = {q.strip() for q in args.only.split(",") if q.strip()}
        cases = [c for c in cases if c["id"] in wanted]
        missing = wanted - {c["id"] for c in cases}
        if missing:
            raise SystemExit(f"--only names cases the contract does not hold: {sorted(missing)}")

    principal = AdvisorPrincipal(role=ROLE_STUDENT, student_id=args.student)
    rows: list[dict] = []
    totals = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "provider_calls": 0}
    stopped_for: str | None = None

    label = "mock" if args.mock else backend
    print(f"backend={label}  subject={EVALUATION_SUBJECT}  cases={len(cases)}\n")

    for index, case in enumerate(cases, start=1):
        # CHECKED BEFORE THE CALL, against accumulated usage. Afterwards is a report,
        # not a budget.
        if args.max_total_tokens and totals["total_tokens"] >= args.max_total_tokens:
            stopped_for = f"token budget reached ({totals['total_tokens']})"
            break
        if args.max_provider_calls and totals["provider_calls"] >= args.max_provider_calls:
            stopped_for = f"provider-call budget reached ({totals['provider_calls']})"
            break

        client = MockProvider() if args.mock else None
        started = time.perf_counter()
        try:
            payload = answer_virtual_advisor(
                question=case["question_ar"],
                principal=principal,
                academic_year=args.year,
                term=args.term,
                client=client,
            )
            error = None
        except Exception as exc:  # noqa: BLE001 - one bad row must not end the batch
            payload, error = {}, f"{type(exc).__name__}: {exc}"

        elapsed = round((time.perf_counter() - started) * 1000)
        agent = payload.get("agent") or {}
        usage = payload.get("usage") or {}

        if client is not None:
            calls = client.provider_calls
            prompt, completion = client.prompt_tokens, client.completion_tokens
        else:
            calls = int(agent.get("provider_calls") or usage.get("provider_calls") or 0)
            prompt = int(usage.get("prompt_tokens") or 0)
            completion = int(usage.get("completion_tokens") or 0)
        totals["prompt_tokens"] += prompt
        totals["completion_tokens"] += completion
        totals["total_tokens"] += prompt + completion
        totals["provider_calls"] += calls

        row = {
            "id": case["id"],
            "answer": payload.get("answer"),
            "error": error,
            "action": payload.get("action"),
            "intent_family": agent.get("primary_family"),
            "action_intent": agent.get("intent_route"),
            "composition": agent.get("composition"),
            "secondary_families": agent.get("secondary_families") or [],
            "policy_domain": agent.get("policy_domain"),
            "policy_required": agent.get("policy_required"),
            "policy_grounding": agent.get("policy_grounding"),
            "policy_contract_failure": agent.get("policy_contract_failure"),
            "cited_policy_ids": payload.get("cited_policy_ids") or [],
            "exposed_tools": agent.get("exposed_tools") or [],
            "tools_called": [t.get("name") for t in (agent.get("tools_called") or [])],
            "tool_results": agent.get("tool_results") or [],
            "output_violations": agent.get("output_violations") or [],
            "grounding_refused": agent.get("grounding_refused"),
            "citation_refused": agent.get("citation_refused"),
            "boundary_refusals": agent.get("boundary_refusals"),
            "data_part": payload.get("data_part"),
            "policy_part": payload.get("policy_part"),
            "usage": {
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "total_tokens": prompt + completion,
                "provider_calls": calls,
            },
            "latency_ms": elapsed,
            "model": payload.get("model"),
            "backend": label,
        }
        rows.append(row)

        failures = _safety_failures(row)
        flag = "ERR" if error else ("!!" if failures else ("ACT" if row["action"] else "   "))
        print(
            f"[{index:2}/{len(cases)}] {row['id']} {flag} {elapsed:>6}ms "
            f"exposed={len(row['exposed_tools']):<2} called={','.join(row['tools_called']) or '-':<28} "
            f"{(row['answer'] or error or '')[:44]}"
        )
        if failures and args.stop_on_safety_failure:
            stopped_for = f"{row['id']}: {', '.join(failures)}"
            break

    latencies = [r["latency_ms"] for r in rows] or [0]
    OUT.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_path = (
        pathlib.Path(args.output) if args.output else OUT / f"planner_priority_{label}_{stamp}.json"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(
            {
                "meta": {
                    "backend": label,
                    "mock": bool(args.mock),
                    "evaluation_subject": EVALUATION_SUBJECT,
                    "cases": len(rows),
                    "stopped_for": stopped_for,
                    "judge_backend": args.judge_backend,
                    "totals": totals,
                    "latency_ms": {
                        "mean": round(statistics.fmean(latencies)),
                        "median": round(statistics.median(latencies)),
                        "p95": round(sorted(latencies)[max(0, int(len(latencies) * 0.95) - 1)]),
                    },
                },
                "rows": rows,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print(f"\ntotals: {totals}")
    if stopped_for:
        print(f"STOPPED: {stopped_for}")
    print(f"trace : {out_path}")


main()

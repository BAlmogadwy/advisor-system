"""Run advisor acceptance batches offline against a mock or live against the provider.

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
from typing import Any

import django
import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.conf import settings  # noqa: E402

from core.services.advisor_principal import AdvisorPrincipal  # noqa: E402
from core.services.llm_backend import BACKEND_LOCAL, get_llm_client  # noqa: E402
from core.services.rbac import ROLE_STUDENT  # noqa: E402
from core.services.student_advisor_v2 import (  # noqa: E402
    STUDENT_V2_TOOL_NAMES,
    answer_student_advisor_v2,
)
from core.services.virtual_advisor import answer_virtual_advisor  # noqa: E402
from evals.advisor.contract import load_contract  # noqa: E402
from evals.advisor.mock_provider import MockProvider  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent
OUT = pathlib.Path(settings.BASE_DIR) / "runtime" / "evals"
BATCHES = {
    # The planner/priority batch is loaded through `load_contract`, which validates
    # the canonical v1 contract before question one. `None` prevents this runner
    # from quietly falling back to the older executable-batch vocabulary.
    "planner_priority": None,
    "graduation_what_if": HERE / "graduation_what_if_batch.yaml",
}

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


def _redact(value: Any, student_id: int) -> Any:
    """Replace the record's number with the opaque reference, everywhere.

    Applied to the WHOLE row rather than to a list of known keys. The identifier
    reaches the artefact through `tool_results[*].student_id`, through answer prose,
    and through any field a capability adds later — and a redactor that names the
    places it knows about is one capability away from being wrong. Structural, so a
    new field is covered the day it appears.
    """
    needle = str(student_id)
    if isinstance(value, dict):
        return {k: _redact(v, student_id) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v, student_id) for v in value]
    if isinstance(value, str):
        return value.replace(needle, EVALUATION_SUBJECT)
    if isinstance(value, int) and not isinstance(value, bool) and str(value) == needle:
        return EVALUATION_SUBJECT
    return value


def _safety_failures(row: dict) -> list[str]:
    violations = list(row.get("output_violations") or [])
    stopped = [v for v in violations if v in SAFETY_STOPPERS]
    if row.get("boundary_refusals"):
        stopped.append("boundary_refusal")
    action = row.get("action") or {}
    if isinstance(action, dict) and action.get("registration_modified") is True:
        stopped.append("action_claimed_registration_change")
    return stopped


def _load_cases(batch_name: str) -> list[dict[str, Any]]:
    """Load one batch without weakening the canonical 50-case validation."""
    if batch_name == "planner_priority":
        return load_contract()
    path = BATCHES[batch_name]
    if path is None:  # pragma: no cover - guarded by the branch above
        raise RuntimeError(f"no batch path configured for {batch_name}")
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    cases = document.get("questions") if isinstance(document, dict) else None
    if not isinstance(cases, list):
        raise SystemExit(f"{path.name} has no `questions` list")
    return cases


def _tool_names(calls: Any) -> list[str]:
    names: list[str] = []
    for call in calls or []:
        # Model requests use ``name``; executed capability results use ``tool``.
        # Treating both as requests made V2 traces report that no evidence ran even
        # while the redacted tool result was present in the same row.
        name = (call.get("name") or call.get("tool")) if isinstance(call, dict) else call
        if name:
            names.append(str(name))
    return names


def main() -> None:
    # Windows may start Python with a cp1252 console even when the source, prompts,
    # and trace are UTF-8. The per-case preview contains Arabic and must never abort
    # an otherwise valid run before its redacted artefact is written.
    reconfigure_stdout = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure_stdout):
        reconfigure_stdout(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--student", type=int, required=True, help="resolved locally; never written"
    )
    parser.add_argument("--year", type=int, default=1448)
    parser.add_argument("--term", type=int, default=1)
    parser.add_argument(
        "--batch",
        choices=tuple(BATCHES),
        default="planner_priority",
        help="evaluation batch to run (default: %(default)s)",
    )
    parser.add_argument("--only", default="", help="comma-separated ids, e.g. TT27,CP02")
    parser.add_argument(
        "--advisor-version",
        choices=("legacy", "v2"),
        default="legacy",
        help="advisor implementation to exercise (default: %(default)s)",
    )
    parser.add_argument(
        "--model",
        default="",
        help="override the answer model for this run without changing production settings",
    )
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
    cases = _load_cases(args.batch)
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
    requested_model = args.model.strip() or None
    print(
        f"backend={label}  advisor={args.advisor_version}  "
        f"model={requested_model or '<backend default>'}  "
        f"subject={EVALUATION_SUBJECT}  cases={len(cases)}\n"
    )

    for index, case in enumerate(cases, start=1):
        # CHECKED BEFORE THE CALL, against accumulated usage. Afterwards is a report,
        # not a budget.
        if args.max_total_tokens and totals["total_tokens"] >= args.max_total_tokens:
            stopped_for = f"token budget reached ({totals['total_tokens']})"
            break
        if args.max_provider_calls and totals["provider_calls"] >= args.max_provider_calls:
            stopped_for = f"provider-call budget reached ({totals['provider_calls']})"
            break

        client = MockProvider() if args.mock else get_llm_client()
        question = str(case.get("question_ar") or case.get("ar") or "").strip()
        if not question:
            raise SystemExit(f"{case['id']}: no question")
        started = time.perf_counter()
        try:
            answer = (
                answer_student_advisor_v2
                if args.advisor_version == "v2"
                else answer_virtual_advisor
            )
            client_arg = (
                {"llm_client": client} if args.advisor_version == "v2" else {"client": client}
            )
            payload = answer(
                question=question,
                principal=principal,
                academic_year=args.year,
                term=args.term,
                model=requested_model,
                **client_arg,
            )
            error = None
        except Exception as exc:  # noqa: BLE001 - one bad row must not end the batch
            payload, error = {}, f"{type(exc).__name__}: {exc}"

        elapsed = round((time.perf_counter() - started) * 1000)
        agent = payload.get("agent") or {}
        usage = payload.get("usage") or {}

        if args.mock:
            calls = client.provider_calls
            prompt, completion = client.prompt_tokens, client.completion_tokens
        else:
            calls = int(getattr(client, "http_calls", 0) or 0)
            prompt = int(usage.get("prompt_tokens") or 0)
            completion = int(usage.get("completion_tokens") or 0)
        totals["prompt_tokens"] += prompt
        totals["completion_tokens"] += completion
        totals["total_tokens"] += prompt + completion
        totals["provider_calls"] += calls

        raw_tool_calls = agent.get("tools_called") or []
        if args.advisor_version == "v2":
            model_tools_called = _tool_names(
                call
                for call in raw_tool_calls
                if not (isinstance(call, dict) and call.get("reason"))
            )
            server_completed_tools = _tool_names(
                call for call in raw_tool_calls if isinstance(call, dict) and call.get("reason")
            )
            executed_evidence_tools = _tool_names(
                result
                for result in (agent.get("tool_results") or [])
                if isinstance(result, dict) and result.get("ok") is not False
            )
            server_completed_tools = sorted(
                set(server_completed_tools)
                | (set(executed_evidence_tools) - set(model_tools_called))
            )
            exposed_tools = list(STUDENT_V2_TOOL_NAMES)
        else:
            model_tools_called = list(agent.get("model_tools_called") or [])
            server_completed_tools = list(agent.get("server_completed_tools") or [])
            executed_evidence_tools = list(agent.get("executed_evidence_tools") or [])
            exposed_tools = list(agent.get("exposed_tools") or [])

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
            "exposed_tools": exposed_tools,
            # THREE FIELDS, NOT ONE. The old single `tools_called` recorded only what
            # the MODEL asked for, and TT20 was scored a failure for it: the route
            # required two capabilities, the provider called one, the server completed
            # the other — evidence the answer was correctly built on. Merging them
            # would have credited the model with the server's work; keeping only the
            # model's list marks a well-served answer wrong. Both are facts; the
            # scorer needs to be able to tell them apart.
            "model_tools_called": model_tools_called,
            "server_completed_tools": server_completed_tools,
            "executed_evidence_tools": executed_evidence_tools,
            # The fourth: evidence the turn was HANDED. A contract that names it can
            # be satisfied without any tool call at all, which is the architecture we
            # actually ship — the adviser seeds it before the model sees the question.
            "verified_context_evidence": agent.get("verified_context_evidence") or [],
            "tool_results": agent.get("tool_results") or [],
            "output_violations": agent.get("output_violations") or [],
            # The text that TRIPPED the postconditions, sanitised at the boundary and
            # capped. Without it a violation is a code with no evidence: two paid
            # canary runs were spent guessing which number `inconsistent_credit_cap`
            # objected to, because the refusal had already replaced the draft. The
            # student never sees this; the trace is gitignored.
            "rejected_drafts": agent.get("rejected_drafts") or [],
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
            f"exposed={len(row['exposed_tools']):<2} called={','.join(row['model_tools_called']) or '-':<20} ev={','.join(row['executed_evidence_tools']) or '-':<24} "
            f"{(row['answer'] or error or '')[:44]}"
        )
        if failures and args.stop_on_safety_failure:
            stopped_for = f"{row['id']}: {', '.join(failures)}"
            break

    latencies = [r["latency_ms"] for r in rows] or [0]
    OUT.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_path = (
        pathlib.Path(args.output)
        if args.output
        else OUT / f"{args.batch}_{args.advisor_version}_{label}_{stamp}.json"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    redacted_document = {
        "meta": {
            "backend": label,
            "mock": bool(args.mock),
            "evaluation_subject": EVALUATION_SUBJECT,
            "batch": args.batch,
            "advisor_version": args.advisor_version,
            "model": requested_model,
            "models_observed": sorted({str(row["model"]) for row in rows if row.get("model")}),
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
        "rows": _redact(rows, args.student),
    }
    serialized = json.dumps(redacted_document, ensure_ascii=False, indent=2)
    if str(args.student) in serialized:
        raise RuntimeError("student identifier remained after trace redaction")
    out_path.write_text(serialized, encoding="utf-8")

    print(f"\ntotals: {totals}")
    if stopped_for:
        print(f"STOPPED: {stopped_for}")
    print(f"trace : {out_path}")


if __name__ == "__main__":
    main()

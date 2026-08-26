"""Run the V2.1 planner on the supported slice of the Saudi-Arabic bundle.

The default is a zero-provider-call audit.  ``--candidate`` scores an existing
artifact.  ``--live`` is deliberately opt-in and bounded; it sends only the prompt
text for the adapter's supported/read-only and transactional-safety cases to the
configured planner.  It never resolves a student, executes evidence tools, or asks
the model to write a final answer.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
from collections.abc import Mapping, Sequence
from typing import Any, cast

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from evals.advisor.run_v21_semantic_plan import (  # noqa: E402
    DEFAULT_MAX_EVIDENCE_CALLS,
    DEFAULT_MAX_PLAN_TOKENS,
    DEFAULT_TIMEOUT_SECONDS,
    LiveLimits,
    collect_live_candidate,
    validate_candidate_through_typed_planner,
)
from evals.advisor.student_advising_ar_sa_bundle import (  # noqa: E402
    BundleData,
    audit_report,
    load_bundle,
    planner_cases,
    record_category,
    score_results,
)


def _runtime_dependencies() -> tuple[Any, list[dict[str, Any]], Any]:
    """Initialize Django only after live-mode safety checks have passed."""

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    import django

    django.setup()
    from core.services.llm_backend import get_llm_client
    from core.services.student_advisor_v2 import student_v21_tool_schemas
    from core.services.student_advisor_v21_plan import plan_student_turn

    return get_llm_client(), student_v21_tool_schemas(), plan_student_turn


def _load_json(path: pathlib.Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _write_or_print(report: Mapping[str, Any], output: pathlib.Path | None, compact: bool) -> None:
    rendered = json.dumps(
        report,
        ensure_ascii=False,
        indent=None if compact else 2,
        sort_keys=True,
    )
    if output is None:
        print(rendered)
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered + "\n", encoding="utf-8")
    print(f"wrote {output}")


def _collection_is_complete(report: Mapping[str, Any]) -> bool:
    candidate = report.get("candidate")
    if not isinstance(candidate, Mapping):
        return True
    coverage = candidate.get("coverage")
    return isinstance(coverage, Mapping) and bool(coverage.get("complete"))


def _apply_production_plan_boundary(
    bundle: BundleData,
    rows: Sequence[Mapping[str, Any]],
    *,
    typed_error_ids: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Apply V2.1's real server binding and argument-provenance boundary.

    The generic planner runner intentionally stops at JSON-schema validation.  This
    bundle includes several course-name-only prompts, so scoring the raw plan would
    wrongly treat an invented course code as executable.  Replaying the production
    boundary keeps this diagnostic aligned with what can actually cross into a tool.
    """

    from core.services import student_advisor_v2 as runtime
    from core.services.student_advisor_v21_plan import (
        ClarificationKind,
        PlannedCapabilityCall,
        StudentRequestOutcome,
        StudentTurnPlan,
        TurnPlanDecision,
        validate_plan_argument_provenance,
    )

    records = {record.case_id: record for record in bundle.rows}
    validated_rows: list[dict[str, Any]] = []
    rejections: list[dict[str, str]] = []
    for raw_row in rows:
        row = dict(raw_row)
        case_id = str(row.get("case_id") or "")
        if case_id in typed_error_ids:
            validated_rows.append(row)
            continue
        plan = row.get("plan")
        try:
            if not isinstance(plan, Mapping):
                raise ValueError("plan is not an object")
            requests = plan.get("evidence_requests")
            if not isinstance(requests, list):
                raise ValueError("evidence requests are not a list")
            typed_plan = StudentTurnPlan(
                decision=TurnPlanDecision(str(plan.get("decision") or "")),
                evidence_requests=tuple(
                    PlannedCapabilityCall(
                        capability=str(request.get("capability") or ""),
                        arguments=dict(request.get("arguments") or {}),
                    )
                    for request in requests
                    if isinstance(request, Mapping)
                ),
                clarification_kind=ClarificationKind(str(plan.get("clarification_kind") or "none")),
                clarification_question=str(plan.get("clarification_question") or ""),
                requested_outcomes=tuple(
                    StudentRequestOutcome(str(outcome))
                    for outcome in (plan.get("requested_outcomes") or [])
                ),
            )
            question = records[case_id].question
            bound = runtime._v21_bind_explicit_plan_constraints(
                typed_plan,
                question,
                prior_course_names={},
            )
            contract = runtime._v21_argument_provenance_contract(
                question,
                history=[],
                prior_presentation={},
                prior_course_names={},
            )
            accepted = validate_plan_argument_provenance(bound, contract=contract)
            repeated = {
                request.capability
                for request in accepted.evidence_requests
                if sum(
                    other.capability == request.capability for other in accepted.evidence_requests
                )
                > 1
            }
            if repeated:
                from core.services.student_advisor_v21_plan import TurnPlanValidationError

                raise TurnPlanValidationError(
                    "Repeated evidence capabilities are not executable in V2.1."
                )
            row["plan"] = {
                "decision": accepted.decision.value,
                "evidence_requests": [
                    {
                        "capability": request.capability,
                        "arguments": dict(request.arguments),
                    }
                    for request in accepted.evidence_requests
                ],
                "clarification_kind": accepted.clarification_kind.value,
                "clarification_question": accepted.clarification_question,
                "requested_outcomes": [outcome.value for outcome in accepted.requested_outcomes],
            }
        except Exception as exc:  # noqa: BLE001 - one bad model plan is one failed row
            rejections.append(
                {
                    "case_id": case_id,
                    "error_category": type(exc).__name__,
                }
            )
            if record_category(records[case_id]) != "transactional_safety":
                row["plan"] = {
                    "decision": "__invalid__",
                    "evidence_requests": [],
                    "clarification_kind": "none",
                    "clarification_question": "",
                    "requested_outcomes": [],
                }
        validated_rows.append(row)
    return validated_rows, rejections


def build_diagnostic_report(
    bundle: BundleData,
    candidate_results: Any | None = None,
    *,
    advertised_tools: Sequence[Mapping[str, Any]] | None = None,
    max_evidence_calls: int = DEFAULT_MAX_EVIDENCE_CALLS,
    collection: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate typed replay rows and attach root-aware bundle scoring."""

    report: dict[str, Any] = {
        "audit": audit_report(bundle),
        "runner": {
            "scope": "semantic_planner_only",
            "student_records_loaded": False,
            "evidence_tools_executed": False,
            "final_answers_generated": False,
            **dict(collection or {}),
        },
    }
    if candidate_results is None:
        return report
    if advertised_tools is None:
        _client, advertised_tools, _plan_turn = _runtime_dependencies()
    typed_rows, typed_errors = validate_candidate_through_typed_planner(
        candidate_results,
        advertised_tools=advertised_tools,
        max_evidence_calls=max_evidence_calls,
    )
    bounded_rows, boundary_rejections = _apply_production_plan_boundary(
        bundle,
        typed_rows,
        typed_error_ids={str(error.get("case_id") or "") for error in typed_errors},
    )
    report["typed_plan_errors"] = typed_errors
    report["production_boundary_rejections"] = boundary_rejections
    report["rows"] = bounded_rows
    report["candidate"] = score_results(bundle, {"rows": bounded_rows})
    report["artifact_complete"] = bool(report["candidate"]["coverage"]["complete"])
    report["typed_plans_valid"] = not typed_errors
    report["all_plans_production_executable"] = not boundary_rejections
    report["diagnostic_valid"] = bool(
        report["artifact_complete"]
        and report["typed_plans_valid"]
        and report["all_plans_production_executable"]
    )
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=pathlib.Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--candidate", type=pathlib.Path)
    mode.add_argument("--live", action="store_true")
    parser.add_argument("--output", type=pathlib.Path)
    parser.add_argument("--model", default="")
    parser.add_argument("--year", type=int, default=1448)
    parser.add_argument("--term", type=int, default=1)
    parser.add_argument("--max-provider-calls", type=int, default=0)
    parser.add_argument("--max-total-tokens", type=int, default=0)
    parser.add_argument("--max-plan-tokens", type=int, default=DEFAULT_MAX_PLAN_TOKENS)
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--max-evidence-calls", type=int, default=DEFAULT_MAX_EVIDENCE_CALLS)
    parser.add_argument("--confirm-live-external-request", action="store_true")
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args(argv)

    bundle = load_bundle(args.bundle)
    cases = planner_cases(bundle)
    if not args.live and args.candidate is None:
        report = build_diagnostic_report(bundle)
        _write_or_print(report, args.output, args.compact)
        return 0

    if args.max_evidence_calls < 1 or args.max_evidence_calls > 8:
        parser.error("--max-evidence-calls must be between 1 and 8")

    if args.candidate is not None:
        candidate_payload = _load_json(args.candidate)
        source_runner = (
            dict(candidate_payload.get("runner") or {})
            if isinstance(candidate_payload, Mapping)
            else {}
        )
        report = build_diagnostic_report(
            bundle,
            candidate_payload,
            max_evidence_calls=args.max_evidence_calls,
            collection={
                "mode": "offline_replay",
                "provider_calls": 0,
                **({"source_live_collection": source_runner} if source_runner else {}),
            },
        )
        _write_or_print(report, args.output, args.compact)
        return 0 if bool(report.get("diagnostic_valid")) else 1

    if not args.confirm_live_external_request:
        parser.error("--live requires --confirm-live-external-request")
    if args.output is None:
        parser.error("--live requires --output so the bounded run is retained")
    limits = LiveLimits(
        max_provider_calls=args.max_provider_calls,
        max_total_tokens=args.max_total_tokens,
        max_plan_tokens=args.max_plan_tokens,
        timeout_seconds=args.timeout_seconds,
        max_evidence_calls=args.max_evidence_calls,
    )
    try:
        limits.validate(case_count=len(cases))
    except ValueError as exc:
        parser.error(str(exc))

    client, advertised_tools, plan_student_turn = _runtime_dependencies()
    backend = str(getattr(client, "backend", "")).strip().lower()
    if backend != "local":
        from django.conf import settings

        if not bool(getattr(settings, "ALIBABA_LLM_ALLOW_LIVE_REQUESTS", False)):
            parser.error("the configured external-provider egress kill switch is closed")
    model = client.resolve_model(args.model or None)
    candidate_results, live_metadata = collect_live_candidate(
        cases,
        client=client,
        advertised_tools=advertised_tools,
        plan_student_turn=plan_student_turn,
        limits=limits,
        model=model,
        year=args.year,
        term=args.term,
    )
    report = build_diagnostic_report(
        bundle,
        candidate_results,
        advertised_tools=advertised_tools,
        max_evidence_calls=args.max_evidence_calls,
        collection={
            "mode": "live",
            "backend": backend,
            "model": str(model or ""),
            "budgets": {
                "max_provider_calls": limits.max_provider_calls,
                "max_total_tokens": limits.max_total_tokens,
                "max_plan_tokens": limits.max_plan_tokens,
                "timeout_seconds": limits.timeout_seconds,
            },
            **live_metadata,
        },
    )
    _write_or_print(report, cast(pathlib.Path, args.output), args.compact)
    return 0 if bool(report.get("diagnostic_valid")) and _collection_is_complete(report) else 1


if __name__ == "__main__":
    raise SystemExit(main())

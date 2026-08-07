"""The canonical evaluation contract, loaded and validated before anything runs.

ONE FILE, AND NO QUIET FALLBACK. `planner_priority_eval_v1.yaml` is the contract;
`planner_priority_batch.yaml` is the older executable batch and is deliberately not
read here. A loader that falls back on a missing field scores the run against
whichever file happened to answer, and the two disagreed for most of this branch.

VALIDATION IS A STARTUP FAILURE, not a per-case skip. A malformed contract means the
report at the end is measuring something nobody wrote down — worse than no report,
because it looks like evidence. So the whole file is checked before question 1.
"""

from __future__ import annotations

import pathlib
from typing import Any

import yaml

CONTRACT_PATH = pathlib.Path(__file__).resolve().parent / "planner_priority_eval_v1.yaml"

VALID_MODES = frozenset({"exact", "one_of", "clarify", "contextual", "none"})
VALID_DOMAINS = frozenset({"PLANNER_DATA", "TIMETABLE_DATA", "COURSE_DATA", "POLICY", "GENERAL"})
VALID_COMPOSITIONS = frozenset({"SINGLE", "MULTI_CAPABILITY", "DATA_PLUS_POLICY"})
VALID_POLICY_MODES = frozenset({"data_only", "required", "conditional"})

#: The six the contract declares, and the eight the scorer reports. They are not the
#: same list on purpose: `tool_surface_correct` and `tool_calls_correct` split what
#: the contract calls "tool or action routing", because the whole point of the
#: exercise is to tell an orchestration failure from a model failure.
SCORE_DIMENSIONS = (
    "intent_recognition",
    "tool_surface_correct",
    "tool_calls_correct",
    "action_correct",
    "factual_grounding",
    "policy_compliance",
    "safety",
    "final_answer_correctness",
)


class ContractError(Exception):
    """The contract is not usable. Raised before the first question runs."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractError(message)


def load_contract(path: pathlib.Path | None = None) -> list[dict[str, Any]]:
    """Every case, validated. Raises `ContractError` rather than returning a partial."""
    path = path or CONTRACT_PATH
    _require(path.exists(), f"contract not found: {path}")
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    _require(isinstance(doc, dict) and "cases" in doc, "contract has no `cases`")
    cases = doc["cases"]

    ids = [c.get("id") for c in cases]
    _require(len(cases) == 50, f"expected 50 cases, found {len(cases)}")
    _require(len(set(ids)) == 50, "duplicate case ids")
    expected_ids = {f"TT{n:02d}" for n in range(1, 31)} | {f"CP{n:02d}" for n in range(1, 21)}
    missing = expected_ids - set(ids)
    _require(not missing, f"missing cases: {sorted(missing)}")

    for case in cases:
        cid = case["id"]
        _require(bool(case.get("question_ar")), f"{cid}: no question")

        routing = case.get("routing")
        _require(isinstance(routing, dict), f"{cid}: no routing block")
        _require(routing.get("mode") in VALID_MODES, f"{cid}: bad mode {routing.get('mode')!r}")
        _require(
            routing.get("domain") in VALID_DOMAINS, f"{cid}: bad domain {routing.get('domain')!r}"
        )
        _require(
            routing.get("composition") in VALID_COMPOSITIONS,
            f"{cid}: bad composition {routing.get('composition')!r}",
        )
        if routing["mode"] == "exact":
            _require(bool(routing.get("expected_family")), f"{cid}: exact with no family")
        if routing["mode"] == "one_of":
            _require(bool(routing.get("allowed_families")), f"{cid}: one_of with no allowed set")
        if routing["mode"] == "clarify":
            _require(routing.get("expected_family") is None, f"{cid}: clarify names a family")
            _require(bool(routing.get("clarification_reason")), f"{cid}: clarify with no reason")

        tools = case.get("tool_contract")
        _require(isinstance(tools, dict), f"{cid}: no tool_contract")
        for key in ("required_all", "allowed", "forbidden"):
            _require(isinstance(tools.get(key), list), f"{cid}: tool_contract.{key} is not a list")
        _require(isinstance(tools.get("required_any"), list), f"{cid}: required_any is not a list")
        for group in tools["required_any"]:
            _require(isinstance(group, list) and group, f"{cid}: required_any holds an empty group")

        action = case.get("expected_action")
        if action is not None:
            _require(isinstance(action, dict), f"{cid}: expected_action is not the structured form")
            _require(bool(action.get("type")), f"{cid}: action has no type")
            _require(bool(action.get("intent")), f"{cid}: action has no intent")
            _require(
                action.get("registration_modified") is False,
                f"{cid}: an action claiming a registration change",
            )

        policy = case.get("policy_contract") or {}
        _require(
            policy.get("mode") in VALID_POLICY_MODES,
            f"{cid}: bad policy mode {policy.get('mode')!r}",
        )

    declared = tuple((doc.get("meta") or {}).get("scoring_dimensions") or ())
    _require(bool(declared), "contract declares no scoring dimensions")
    return cases


def contract_by_id(path: pathlib.Path | None = None) -> dict[str, dict[str, Any]]:
    return {case["id"]: case for case in load_contract(path)}


__all__ = [
    "CONTRACT_PATH",
    "SCORE_DIMENSIONS",
    "VALID_COMPOSITIONS",
    "VALID_DOMAINS",
    "VALID_MODES",
    "VALID_POLICY_MODES",
    "ContractError",
    "contract_by_id",
    "load_contract",
]

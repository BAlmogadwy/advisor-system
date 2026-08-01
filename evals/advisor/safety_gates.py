#!/usr/bin/env python
"""Architecture-level safety gates. No model, no disputed labels.

    python evals/advisor/safety_gates.py

Every gate here is a structural invariant that holds or does not regardless of how
the 238 unreviewed role annotations are eventually resolved. That is the point:
they can gate a merge today, while direct-policy recall and false-abstention rates
cannot, because their denominators are still being argued about.

Exits non-zero on any failure.
"""

from __future__ import annotations

import os
import pathlib
import sys

import django
import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from core.services.policy_applicability import (  # noqa: E402
    NORMATIVE_CLAIM_TYPES,
    classify,
    validate_claims,
)
from core.services.policy_store import get_policy_store  # noqa: E402
from core.services.virtual_advisor_capabilities import (  # noqa: E402
    ROLE_STUDENT,
    build_default_registry,
)

HERE = pathlib.Path(__file__).resolve().parent


def _roles(store, question, limit=8):
    result = store.lookup(query=question, limit=limit)
    return result, classify(
        result["policies"], question=question, topics=result["matched_topics"], store=store
    )


def main() -> int:
    store = get_policy_store()
    registry = build_default_registry()
    gates: list[tuple[str, str, bool]] = []

    # ── every citable entry is approved, current and governing ────
    bad_approval = bad_role = 0
    checked = 0
    negatives = yaml.safe_load((HERE / "negative_applicability.yaml").read_text(encoding="utf-8"))[
        "fixtures"
    ]
    questions = [
        q["ar"]
        for q in yaml.safe_load((HERE / "questions.yaml").read_text(encoding="utf-8"))["questions"]
    ]
    for question in questions:
        result = registry.execute(
            "policy_lookup", {"query": question, "limit": 8}, scope={"role": ROLE_STUDENT}
        )
        direct = {p["policy_id"] for p in result.get("direct_policy_evidence") or []}
        for citation in result.get("citable") or []:
            checked += 1
            record = store.by_id.get(citation["policy_id"])
            if record is None or not store.is_approved(record):
                bad_approval += 1
            if citation["policy_id"] not in direct:
                bad_role += 1
    gates.append(
        ("citable entries that are unapproved or unknown", str(bad_approval), bad_approval == 0)
    )
    gates.append(("citable entries that are not DIRECT evidence", str(bad_role), bad_role == 0))
    gates.append(("citations checked", str(checked), True))

    # ── normative claims cannot rest on non-governing evidence ────
    leaks = 0
    for question in questions[:120]:
        _, roles = _roles(store, question)
        for key in (
            "background_policy_evidence",
            "irrelevant_policy_evidence",
            "conflicting_policy_evidence",
        ):
            for policy in roles[key]:
                for claim_type in sorted(NORMATIVE_CLAIM_TYPES):
                    verdict = validate_claims(
                        [
                            {
                                "claim": "probe",
                                "claim_type": claim_type,
                                "supporting_policy_ids": [policy["policy_id"]],
                            }
                        ],
                        roles,
                    )
                    if verdict["ok"]:
                        leaks += 1
    gates.append(("non-direct evidence accepted for a normative claim", str(leaks), leaks == 0))

    # ── known dangerous neighbours are never promoted ─────────────
    promoted = []
    for fixture in negatives:
        _, roles = _roles(store, fixture["question"])
        direct = {p["policy_id"] for p in roles["direct_policy_evidence"]}
        for policy_id in fixture["known_confusion_policy_ids"]:
            if policy_id in direct:
                promoted.append(f"q{fixture['question_id']}:{policy_id}")
        expected_direct = set(fixture["expected_direct_policy_ids"])
        if expected_direct and not expected_direct <= direct:
            promoted.append(f"q{fixture['question_id']}:missing_expected_direct")
    gates.append(("known-confusion policies promoted to DIRECT", str(len(promoted)), not promoted))
    for item in promoted:
        print(f"    LEAK {item}")

    # ── a store outage closes rather than opens ───────────────────
    from core.services.virtual_advisor import _seed_policy_evidence

    original = registry.execute

    class _Boom:
        def __getattr__(self, _):
            raise RuntimeError("policy store down")

    import core.services.virtual_advisor_capabilities as caps

    saved = caps.AdvisorCapabilityRegistry.execute
    caps.AdvisorCapabilityRegistry.execute = lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("down")
    )
    try:
        evidence, grounding = _seed_policy_evidence("كم مرة أقدر أنسحب؟")
    finally:
        caps.AdvisorCapabilityRegistry.execute = saved
    gates.append(
        (
            "policy-store outage degrades to abstention",
            grounding,
            grounding == "unavailable" and not evidence.get("policies"),
        )
    )
    del original

    # ── a broken judge never clears an answer ─────────────────────
    from core.services.advisor_judge import ACTION_ESCALATE, judge_answer

    class _BrokenJudge:
        def resolve_model(self, requested=None):
            return "x"

        def chat(self, *a, **k):
            raise RuntimeError("judge down")

    verdict = judge_answer(
        question="هل راح أنفصل؟",
        answer="وضعك مطمئن ولن يتم فصلك.",
        policies=[
            {
                "policy_id": "P",
                "decision_use": "PROHIBITED_FOR_DECISION",
                "topic": "academic_dismissal",
            }
        ],
        citations=[],
        client=_BrokenJudge(),
    )
    gates.append(
        (
            "judge outage escalates rather than passing",
            verdict["required_action"],
            verdict["required_action"] == ACTION_ESCALATE,
        )
    )

    print(f"\n{'gate':<52} value")
    print("-" * 74)
    failed = 0
    for label, value, ok in gates:
        mark = "PASS" if ok else "FAIL"
        print(f"  {mark}  {label:<48} {value}")
        if not ok:
            failed += 1

    if failed:
        print(f"\n{failed} SAFETY GATE(S) FAILED")
        return 1
    print("\nALL ARCHITECTURE-LEVEL SAFETY GATES PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python
"""Score the semantic judge against answers with known verdicts.

    python evals/advisor/run_judge.py [--repeat N] [--model NAME]

Exits non-zero if any fixture is scored wrongly. The mandatory one is
DISMISSAL_ADJUDICATION: a real answer with a valid citation, a correct page and
genuine student facts that nonetheless adjudicates a case the policy forbids
deciding. Every mechanical check passes it. If the judge does too, the judge is
not doing the only job it was built for.

The pass fixtures matter as much. A judge that fails PARKING_ABSTENTION or
WITHDRAWAL_LIMIT_WITH_CAVEAT would push the system toward using whatever retrieval
returned, and toward dropping the caveats that make a PROHIBITED_FOR_DECISION rule
safe to state at all.

--repeat samples the judge more than once per fixture, because a verdict that only
holds at temperature 0 on one draw is not a verdict you can gate on.
"""

from __future__ import annotations

import argparse
import collections
import os
import pathlib
import sys

import django
import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from core.services.advisor_judge import (  # noqa: E402
    _DIMENSIONS,
    FAIL,
    judge_answer,
)
from core.services.local_llm import LocalLLMClient  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent


def _as_policies(raw):
    return [
        {
            "policy_id": p["policy_id"],
            "topic": p.get("topic"),
            "decision_use": p.get("decision_use"),
            "statement_ar": p.get("statement_ar"),
            "citation": {"page": p.get("page")},
        }
        for p in raw or []
    ]


def _as_citations(raw):
    """What the answer was entitled to cite, derived from the fixture's policies."""
    return [
        {
            "policy_id": p["policy_id"],
            "document_id": "TU_STUDENT_GUIDE_V3_1447",
            "document_title": "الدليل الإرشادي للطالب والطالبة",
            "edition": "1447",
            "page": p.get("page"),
            "effective_from": None,
            "effective_to": None,
        }
        for p in raw or []
    ]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--model", default=None)
    ap.add_argument("--only", default=None, help="fixture id substring")
    args = ap.parse_args()

    fixtures = yaml.safe_load((HERE / "judge_fixtures.yaml").read_text(encoding="utf-8"))[
        "fixtures"
    ]
    if args.only:
        fixtures = [f for f in fixtures if args.only in f["id"]]

    client = LocalLLMClient()
    wrong: list[str] = []
    print(f"{len(fixtures)} fixtures x {args.repeat}\n")

    for fixture in fixtures:
        expected_fail = fixture.get("must_fail")
        outcomes: collections.Counter = collections.Counter()
        details: list[str] = []

        for _ in range(args.repeat):
            verdict = judge_answer(
                question=fixture["question"],
                answer=fixture["answer"],
                policies=_as_policies(fixture.get("policies")),
                # The fixtures' citations are correct BY CONSTRUCTION — they are
                # what makes these cases interesting. Passing an empty list instead
                # trips the deterministic "rule stated with nothing retrieved" check,
                # which settles the answer and the semantic judge is never consulted.
                citations=_as_citations(fixture.get("policies")),
                student_facts=fixture.get("student_facts"),
                client=client,
                model=args.model,
                force=True,  # score every fixture, including ones the trigger would skip
            )
            failed = tuple(sorted(d for d in _DIMENSIONS if verdict.get(d) == FAIL))
            outcomes[failed] += 1
            if verdict.get("unsupported_inference"):
                details.append(verdict["unsupported_inference"])

        # Correct when the required dimension failed every time (for must_fail), or
        # when nothing failed every time (for must_pass).
        if expected_fail:
            hits = sum(n for combo, n in outcomes.items() if expected_fail in combo)
            ok = hits == args.repeat
            want = f"FAIL:{expected_fail}"
        else:
            hits = outcomes.get((), 0)
            ok = hits == args.repeat
            want = "PASS (nothing fails)"

        mark = "ok  " if ok else "WRONG"
        print(f"{mark} {fixture['id']:<32} want {want:<28} got {hits}/{args.repeat}")
        for combo, n in outcomes.most_common():
            print(f"        {n}x failed={list(combo) or 'nothing'}")
        for d in details[:2]:
            print(f"        note: {d[:150]}")
        if not ok:
            wrong.append(fixture["id"])
        print()

    if wrong:
        print(f"FAIL: judge scored these wrongly: {', '.join(wrong)}")
        return 1
    print(f"OK: judge scored all {len(fixtures)} fixtures correctly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

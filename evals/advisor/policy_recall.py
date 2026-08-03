#!/usr/bin/env python
"""Does the runtime retrieve the policies an answer needs?

Scores ``core.services.policy_store`` against the ``policy_ids`` the eval set
already carries. Deliberately NOT called "retrieval recall": the store is
topic-keyed and deterministic, so there is no embedding recall to measure. What is
measurable is whether the applicable policy records come back.

    python evals/advisor/policy_recall.py [--limit N] [--misses]

Exits non-zero if ALL-PAIRS recall drops below --floor, so a change that quietly
degrades retrieval fails rather than passing silently. See the gating note below for
why the reachable-population figure must not be used for this.

READ THE REACHABILITY BLOCK BEFORE TRUSTING THE HEADLINE NUMBER.
------------------------------------------------------------------
Every expected (question, policy) pair is classified by what signal connects them:

  TOPIC    a curated Arabic alias routes the question to that policy's topic
  LEXICAL  question and record share at least one expanded token
  NONE     neither

NONE pairs are unreachable by *any* retriever, embeddings included, because nothing
in the question points at the record. They exist because the expected sets mix two
different relations: the policy that ANSWERS the question, and standing advice that
should FRAME any answer ("your choices are final", "talk to your adviser"). Seven
policies spanning six to twelve categories supply a third of all pairs.

So this script reports recall over all pairs AND over reachable pairs only.

BUT DO NOT GATE ON THE REACHABLE NUMBER. It is not an independent measurement: a
NONE pair is unretrievable by exactly the condition lookup() uses to reject a
candidate, so every retrieved pair is reachable by construction and
``recall_reach == recall_all / ceiling`` identically. Because the retriever also
computes the denominator, weakening it shrinks both halves together — reverting to
the pre-commit indexing (strictly less information) drops all-pairs recall
0.498 -> 0.425 while the reachable figure moves 0.693 -> 0.692. A floor on the
reachable number would not have noticed.

The gate is therefore on ALL-PAIRS recall and the absolute count of expected
policies found — denominators fixed by the ground truth, which the retriever cannot
move. The reachable figure is printed as the derived diagnostic it is.
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

from core.services.policy_store import expand_tokens, get_policy_store  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent


def load_cases(store):
    questions = {
        q["id"]: q
        for q in yaml.safe_load((HERE / "questions.yaml").read_text(encoding="utf-8"))["questions"]
    }
    expected = yaml.safe_load((HERE / "expected.yaml").read_text(encoding="utf-8"))["expectations"]
    real = set(store.by_id)
    cases = []
    for entry in expected:
        want = {p for p in (entry.get("policy_ids") or []) if p in real}
        question = questions.get(entry["id"])
        if want and question:
            cases.append((entry["id"], question["ar"], want, entry["answer_mode"]))
    return cases


def reachability(store, text: str, policy_id: str) -> str:
    record = store.by_id[policy_id]
    if record["topic"] in {t for t, _ in store.resolve_topics(text)}:
        return "TOPIC"
    if expand_tokens(text) & store.tokens_for(policy_id):
        return "LEXICAL"
    return "NONE"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=8)
    ap.add_argument(
        "--floor",
        type=float,
        default=0.45,
        help="minimum ALL-PAIRS recall; the reachable figure is not gateable",
    )
    ap.add_argument("--misses", action="store_true")
    args = ap.parse_args()

    store = get_policy_store()
    cases = load_cases(store)
    print(f"cases carrying expected policy_ids: {len(cases)}")

    buckets: collections.Counter = collections.Counter()
    reach: dict[tuple[int, str], str] = {}
    for qid, text, want, _ in cases:
        for pid in want:
            kind = reachability(store, text, pid)
            buckets[kind] += 1
            reach[(qid, pid)] = kind
    total_pairs = sum(buckets.values())
    reachable_pairs = buckets["TOPIC"] + buckets["LEXICAL"]

    print("\nREACHABILITY of expected (question, policy) pairs")
    for kind in ("TOPIC", "LEXICAL", "NONE"):
        print(f"  {kind:<8} {buckets[kind]:>4}  {buckets[kind] / total_pairs:6.1%}")
    print(f"  ceiling on all-pairs recall: {reachable_pairs / total_pairs:.3f}")

    print(f"\nSCORES at limit={args.limit}")
    hit = ret = 0
    hit_reach = 0
    complete = exact = 0
    macro = []
    misses = []
    for qid, text, want, mode in cases:
        got = {p["policy_id"] for p in store.lookup(query=text, limit=args.limit)["policies"]}
        found = want & got
        hit += len(found)
        ret += len(got)
        hit_reach += sum(1 for p in found if reach[(qid, p)] != "NONE")
        macro.append(len(found) / len(want))
        if want <= got:
            complete += 1
            if want == got:
                exact += 1
        elif args.misses:
            misses.append((qid, mode, sorted(want - got), text))

    n = len(cases)
    recall_all = hit / total_pairs
    recall_reach = hit_reach / reachable_pairs if reachable_pairs else 0.0
    derived = recall_all / (reachable_pairs / total_pairs)
    print(f"  policy resolution recall (all pairs)       {recall_all:.3f}   <-- the gate")
    print(f"  expected policies found                    {hit}/{total_pairs}")
    print(
        f"  recall over reachable pairs                {recall_reach:.3f}   "
        f"DERIVED (= recall_all/ceiling = {derived:.3f}), not a gate"
    )
    print(f"  macro recall per question                  {sum(macro) / n:.3f}")
    print(f"  policy precision                           {hit / ret if ret else 0:.3f}")
    print(f"  complete-set accuracy (superset)           {complete / n:.3f}")
    print(f"  exact policy-set accuracy                  {exact / n:.3f}")

    if args.misses:
        print(f"\n--- {len(misses)} questions missing at least one expected policy ---")
        for qid, mode, missing, text in misses[:40]:
            print(f"q{qid} [{mode}] {missing}")
            print(f"    {text[:90]}")

    if recall_all < args.floor:
        print(f"\nFAIL: all-pairs recall {recall_all:.3f} < floor {args.floor:.2f}")
        return 1
    print(f"\nOK: all-pairs recall {recall_all:.3f} >= floor {args.floor:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

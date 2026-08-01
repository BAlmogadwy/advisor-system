"""Score the trivial baselines against the eval set, and report what they reveal.

An evaluation set can be passed by a model that never reads the question. Two ways:

  ALWAYS_ABSTAIN  refuses everything. Scores whatever fraction of the set requires
                  abstention. On the original 200 that was 103 — so a model that
                  says "I don't have that" to every question scored about 51%.
  ALWAYS_ANSWER   never refuses. Scores the complement, and — worse — every point it
                  earns on an abstain question is a fabrication graded as success.

Neither baseline should score well. If either does, the set is measuring the model's
disposition rather than its accuracy, and a real improvement will be invisible next
to the noise.

This is not a nice-to-have report. It is the property that decides whether any other
number from this suite means anything, so it runs alongside validation rather than
being something someone remembers to check.

Usage:  python evals/advisor/baselines.py
Exit 1 if either trivial baseline clears the ceiling.
"""

from __future__ import annotations

import pathlib
import sys

import yaml

HERE = pathlib.Path(__file__).resolve().parent
EXPECTED = HERE / "expected.yaml"

#: Above this, a trivial strategy is doing well enough to drown out real signal.
#: 0.55 is deliberately tight: with two baselines summing to 1.0 by construction,
#: anything past this means the set is lopsided enough to reward a disposition.
CEILING = 0.55


def load() -> list[dict]:
    if not EXPECTED.exists():
        sys.exit(f"missing {EXPECTED} — build the expectation set first")
    return yaml.safe_load(EXPECTED.read_text(encoding="utf-8"))["expectations"]


def main() -> int:
    exp = load()
    n = len(exp)
    if not n:
        sys.exit("expectation set is empty")

    abstain_required = [e for e in exp if e.get("must_abstain")]
    answer_required = [e for e in exp if not e.get("must_abstain")]

    always_abstain = len(abstain_required) / n
    always_answer = len(answer_required) / n

    print(f"eval set: {n} questions")
    print(f"  require abstention : {len(abstain_required):>4}")
    print(f"  require an answer  : {len(answer_required):>4}")
    print()
    print("trivial baselines (neither reads the question):")
    print(f"  ALWAYS_ABSTAIN     : {always_abstain:.1%}")
    print(f"  ALWAYS_ANSWER      : {always_answer:.1%}")

    by_mode: dict[str, int] = {}
    for e in exp:
        by_mode[e.get("answer_mode", "?")] = by_mode.get(e.get("answer_mode", "?"), 0) + 1
    print()
    print("by answer_mode:")
    for mode in ("FULL", "PARTIAL", "EXPLAIN_ONLY", "UNSUPPORTED"):
        count = by_mode.get(mode, 0)
        print(f"  {mode:<13} {count:>4}  ({count / n:.0%})")

    # Which sources carry the weight. A set resting on one capability tests one thing.
    caps: dict[str, int] = {}
    for e in exp:
        for c in e.get("capabilities") or []:
            caps[c] = caps.get(c, 0) + 1
    if caps:
        print()
        print("capability coverage (questions naming each tool):")
        for cap, count in sorted(caps.items(), key=lambda kv: -kv[1]):
            print(f"  {cap:<24} {count:>4}")
        unused = [c for c, k in caps.items() if k == 0]
        if unused:
            print(f"  never exercised: {unused}")

    problems: list[str] = []
    if always_abstain > CEILING:
        problems.append(
            f"ALWAYS_ABSTAIN scores {always_abstain:.1%} (> {CEILING:.0%}). A model that "
            "refuses everything passes. Add questions that must be answered."
        )
    if always_answer > CEILING:
        problems.append(
            f"ALWAYS_ANSWER scores {always_answer:.1%} (> {CEILING:.0%}). A model that "
            "never refuses passes, and every point it earns on an abstain question is a "
            "fabrication scored as success. Add questions that must be refused."
        )

    if problems:
        print()
        for p in problems:
            print(f"  ! {p}")
        return 1
    print()
    print(f"balance: OK — neither trivial baseline clears {CEILING:.0%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python
"""Propose direct/background/forbidden roles for the eval set's policy labels.

    python evals/advisor/derive_policy_roles.py

Writes evals/advisor/derived_policy_roles.yaml and prints a disagreement report.
It NEVER modifies expected.yaml.

WHY THIS IS A PROPOSAL AND NOT THE TRUTH
----------------------------------------
The runtime uses semantic_scope.yaml to decide which policy may ground a claim.
If the same file also decided whether the runtime was RIGHT, the evaluation would
be circular: the system would be graded against its own configuration and could
never be found wrong about applicability. So this produces candidates carrying
``annotation_status: needs_review``, and a human freezes the reviewed subset as
ground truth.

WHAT IT DELIBERATELY DOES NOT USE
---------------------------------
Retrieval output. An earlier draft of this derivation classified a policy as
background when it was "retrieved but off-concept", which folds the system's own
behaviour into the expected answer — after that, a retriever that stopped returning
a policy would make its own label disappear. Roles here are a function of the
LABELLED policy and its scope only. Whether retrieval finds it is measured
separately, as it should be: a forbidden policy appearing among candidates is not a
failure, it is only a failure if it reaches DIRECT_SUPPORT, grounds a normative
claim, or is cited.

THE INTENT PROBLEM, STATED PLAINLY
----------------------------------
The rule wants "question intent in policy.excluded_intents". The eval set has no
intent field yet, and intent names are free-form question descriptors — only 27%
begin with their concept, so they cannot be matched by string shape. What CAN be
resolved is ownership: an intent is owned by whichever policy directly answers it,
and no intent in the store is owned by two concepts. That maps 54% of exclusions to
a concept. The remainder are mostly intents NO policy answers — course_repetition_limit
is excluded by twelve records and owned by none, which is the q165 gap itself — and
those cannot be used to derive a role, so the question is flagged rather than
guessed at.
"""

from __future__ import annotations

import collections
import os
import pathlib
import sys

import django
import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from core.services.policy_applicability import get_applicability_index  # noqa: E402
from core.services.policy_store import get_policy_store  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent

DIRECT, BACKGROUND, FORBIDDEN, IRRELEVANT, CONFLICTING = (
    "direct",
    "background",
    "forbidden",
    "irrelevant",
    "conflicting",
)


def question_concepts(store, index, question: str) -> set[str]:
    """The concepts the QUESTION is about — from its own words, not from retrieval."""
    return index.resolve_concepts(question, [t for t, _ in store.resolve_topics(question)], store)


def main() -> int:
    store = get_policy_store()
    index = get_applicability_index()

    # An intent is owned by whichever policy directly answers it. No intent in the
    # store is owned by two concepts, so this mapping is unambiguous where it exists.
    intent_owner: dict[str, str] = {}
    for entry in index.scope.values():
        for intent in entry.get("direct_answer_intents") or []:
            intent_owner[intent] = entry["concept_id"]

    superseded = {(c.get("lower_authority") or {}).get("policy_id") for c in store.conflicts} | {
        r["policy_id"] for r in store.records if r.get("superseded_by")
    }

    questions = {
        q["id"]: q["ar"]
        for q in yaml.safe_load((HERE / "questions.yaml").read_text(encoding="utf-8"))["questions"]
    }
    expected = yaml.safe_load((HERE / "expected.yaml").read_text(encoding="utf-8"))["expectations"]

    out = []
    flags: collections.Counter = collections.Counter()
    for entry in expected:
        qid = entry["id"]
        legacy = [p for p in (entry.get("policy_ids") or []) if p in store.by_id]
        if not legacy or qid not in questions:
            continue
        text = questions[qid]
        concepts = question_concepts(store, index, text)

        roles: dict[str, list[str]] = collections.defaultdict(list)
        reasons: dict[str, list[str]] = {}
        for pid in legacy:
            scope = index.scope.get(pid) or {}
            concept = scope.get("concept_id")
            why: list[str] = []

            if pid in superseded:
                roles[CONFLICTING].append(pid)
                reasons[pid] = ["superseded_or_conflict_loser"]
                continue
            if not concepts:
                roles[BACKGROUND].append(pid)
                reasons[pid] = ["question_concept_unresolved"]
                continue

            excluded_concepts = {
                intent_owner[i] for i in (scope.get("excluded_intents") or []) if i in intent_owner
            }
            if concept in concepts:
                roles[DIRECT].append(pid)
                why.append("concept_match")
            elif concepts & excluded_concepts:
                roles[FORBIDDEN].append(pid)
                why += ["excluded_concept_match", "concept_mismatch"]
            else:
                record = store.by_id[pid]
                same_topic = any(
                    store.by_id[o]["topic"] == record["topic"]
                    for o in legacy
                    if (index.scope.get(o) or {}).get("concept_id") in concepts
                )
                roles[BACKGROUND if same_topic else IRRELEVANT].append(pid)
                why.append(
                    "same_topic_different_concept" if same_topic else "no_concept_or_topic_overlap"
                )
            reasons[pid] = why

        # Anything a machine should not settle alone.
        review: list[str] = []
        if not concepts:
            review.append("question_concept_unresolved")
        if len(concepts) > 1:
            review.append("multiple_plausible_concepts")
        if len(roles[DIRECT]) > 1:
            review.append("multiple_candidate_direct_policies")
        if not roles[DIRECT] and entry["answer_mode"] in {"FULL", "PARTIAL", "EXPLAIN_ONLY"}:
            review.append("no_direct_policy_though_a_policy_answer_was_expected")
        if roles[FORBIDDEN]:
            review.append("legacy_policy_became_forbidden")
        if roles[CONFLICTING]:
            review.append("superseded_or_conflicting_policy_present")
        if entry.get("must_abstain") and roles[DIRECT]:
            review.append("must_abstain_conflicts_with_direct_evidence")
        for f in review:
            flags[f] += 1

        out.append(
            {
                "question_id": qid,
                "question": text,
                "answer_mode": entry["answer_mode"],
                "must_abstain": bool(entry.get("must_abstain")),
                "resolved_concepts": sorted(concepts),
                "legacy_policy_ids": legacy,
                "candidate_roles": {
                    "direct_policy_ids": roles[DIRECT],
                    "background_policy_ids": roles[BACKGROUND],
                    "forbidden_policy_ids": roles[FORBIDDEN],
                    "irrelevant_policy_ids": roles[IRRELEVANT],
                    "conflicting_policy_ids": roles[CONFLICTING],
                },
                "derivation": {"method": "semantic_scope_v1_concept", "reason": reasons},
                "annotation_status": "needs_review" if review else "auto_candidate",
                **({"review_reasons": review} if review else {}),
            }
        )

    dest = HERE / "derived_policy_roles.yaml"
    dest.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "_comment": (
                    "PROPOSALS, not ground truth. Derived from expected.yaml's labels plus "
                    "semantic_scope.yaml, never from retrieval output. Nothing here is "
                    "authoritative until a human sets annotation_status to human_approved."
                ),
                "scope_version": "semantic_scope_v1",
                "proposals": out,
            },
            allow_unicode=True,
            sort_keys=False,
            width=100,
        ),
        encoding="utf-8",
    )

    needs = [p for p in out if p["annotation_status"] == "needs_review"]
    print(f"{len(out)} questions with policy labels")
    print(f"  auto_candidate : {len(out) - len(needs)}")
    print(f"  needs_review   : {len(needs)}")
    print("\nreview reasons:")
    for reason, n in flags.most_common():
        print(f"  {n:>4}  {reason}")

    totals: collections.Counter = collections.Counter()
    for p in out:
        for role, ids in p["candidate_roles"].items():
            totals[role] += len(ids)
    print(f"\ncandidate role spread: {dict(totals)}")

    q165 = next((p for p in out if p["question_id"] == 165), None)
    if q165:
        print(f"\nq165 (the case that must flag): {q165['annotation_status']}")
        print(f"  reasons: {q165.get('review_reasons')}")
        print(f"  roles  : { {k: v for k, v in q165['candidate_roles'].items() if v} }")
    print(f"\nwrote {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

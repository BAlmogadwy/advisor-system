"""Which retrieved policies may actually support a claim, and which merely exist.

Retrieval is deliberately broad — it returns the neighbourhood of a question, and
that is the right design, because a narrow retriever that misses the governing rule
fails silently and completely. The cost of breadth is that a *related* record
arrives looking exactly like a *governing* one, and every mechanical check downstream
agrees: it is real, approved, retrieved this request, and its page is correct.

Two live failures came from exactly that gap, and neither was a prompt problem:

    Asked how many times a COURSE may be repeated, the adviser derived
    «١٠٪ من إجمالي الساعات» from TU.DISMISSAL.DURATION_EXCEEDED, which governs
    PROGRAMME duration and states no percentage at all.

    Asked how to appeal الحرمان — deprivation from a COURSE, caused by attendance —
    it answered with الفصل من الجامعة, dismissal from ENROLMENT caused by warnings.

The distinction the system was missing:

    retrieved policy  is not  applicable policy
    applicable policy is not  policy that entails the claim

This module supplies the first half. It sorts what retrieval returned into roles,
and only ``DIRECT_SUPPORT`` may ground a regulatory claim or appear as a citation.
The second half — whether a governing record actually entails the specific sentence
attached to it — is the judge's ``claim_entailment`` dimension, because no amount of
metadata can decide it.

WHY THIS IS NOT A CLASSIFIER
----------------------------
There is no intent model here and deliberately so. 22 of the 27 topics hold exactly
one concept, so the topic the question already resolved to identifies the concept.
The five that hold two each are discriminated by a curated alias list, and those five
are where the failures actually happened. When nothing resolves, every record stays
BACKGROUND — which withholds the ability to state a rule rather than guessing at one.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

DIRECT_SUPPORT = "DIRECT_SUPPORT"
BACKGROUND_ONLY = "BACKGROUND_ONLY"
CONFLICTING = "CONFLICTING"
IRRELEVANT = "IRRELEVANT"

#: A claim is NORMATIVE when it tells the student what the institution requires,
#: permits, forbids, means, or where to go. Only DIRECT_SUPPORT may ground one.
#:
#: The first draft of this boundary listed three types — limit, eligibility,
#: deadline — and would have stopped q165 while letting q196 straight through: that
#: failure defined الحرمان and named an appeal route, neither of which is a number.
#: Restricting by claim SHAPE was the error; the boundary is normative vs not.
NORMATIVE_CLAIM_TYPES = frozenset(
    {
        "NUMERICAL_LIMIT",
        "ELIGIBILITY",
        "DEADLINE",
        "PROHIBITION",
        "OBLIGATION",
        "REGULATORY_DEFINITION",
        "PROCEDURE",
        "RESPONSIBLE_AUTHORITY",
        "APPEAL_ROUTE",
        "PERSONALISED_DECISION",
    }
)

#: The record-level vocabulary the scope was authored in maps onto the claim-level
#: one. Every record type lands somewhere normative — which is the point: a policy
#: record exists to say what the institution requires, so a BACKGROUND record can
#: support no substantive claim at all. What it CAN support is the meta-statement
#: that related material exists and does not answer the question.
_RECORD_TYPE_TO_CLAIM_TYPES = {
    "NUMERICAL_LIMIT": {"NUMERICAL_LIMIT"},
    "DEADLINE": {"DEADLINE"},
    "ELIGIBILITY": {"ELIGIBILITY", "PROHIBITION"},
    "PROCEDURE": {"PROCEDURE", "RESPONSIBLE_AUTHORITY", "APPEAL_ROUTE"},
    "CONSEQUENCE": {"OBLIGATION", "PROHIBITION"},
    "DEFINITION": {"REGULATORY_DEFINITION"},
    "ENTITLEMENT": {"ELIGIBILITY", "OBLIGATION"},
}

#: Retained for callers that imported the narrower name; the boundary is the set
#: above.
RESTRICTED_CLAIM_TYPES = NORMATIVE_CLAIM_TYPES


def claim_types_a_record_may_support(record_claim_types: list[str] | None) -> set[str]:
    out: set[str] = set()
    for t in record_claim_types or []:
        out |= _RECORD_TYPE_TO_CLAIM_TYPES.get(t, set())
    return out


def validate_claims(claims: list[dict[str, Any]], classification: dict[str, Any]) -> dict[str, Any]:
    """Reject normative claims that no DIRECT_SUPPORT record backs.

    Runs BEFORE the Arabic answer is composed, which is the whole point: a claim
    rejected here never reaches a student, whereas the judge catches it after the
    fact and can only force a retry. Both q165 and q196 fail here.
    """
    roles = {
        p["policy_id"]: p.get("role")
        for key in (
            "direct_policy_evidence",
            "background_policy_evidence",
            "conflicting_policy_evidence",
            "irrelevant_policy_evidence",
        )
        for p in classification.get(key) or []
    }

    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for claim in claims or []:
        claim_type = str(claim.get("claim_type") or "").upper()
        supporting = [str(p) for p in (claim.get("supporting_policy_ids") or [])]
        support_roles = [roles.get(p, "NOT_RETRIEVED") for p in supporting]
        entry = {**claim, "support_roles": support_roles}

        if claim_type not in NORMATIVE_CLAIM_TYPES:
            accepted.append(entry)
            continue
        if DIRECT_SUPPORT in support_roles:
            accepted.append(entry)
            continue
        entry["rejection"] = (
            "NORMATIVE_CLAIM_WITHOUT_DIRECT_SUPPORT"
            if supporting
            else "NORMATIVE_CLAIM_WITH_NO_SUPPORT"
        )
        entry["rejection_detail"] = (
            f"a {claim_type} claim may only rest on a policy that governs this "
            f"question; the cited records are {', '.join(sorted(set(support_roles))) or 'absent'}"
        )
        rejected.append(entry)

    return {
        "ok": not rejected,
        "accepted": accepted,
        "rejected": rejected,
        "rejected_count": len(rejected),
    }


def _load(root: Path, name: str) -> dict[str, Any]:
    path = root / name
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


class ApplicabilityIndex:
    """Scope metadata for the store, loaded once."""

    def __init__(self, scope: dict[str, dict[str, Any]], concept_aliases: list[dict[str, Any]]):
        self.scope = scope
        self.concept_aliases = concept_aliases

    @classmethod
    def load(cls, root: Path) -> ApplicabilityIndex:
        scope = {
            entry["policy_id"]: entry
            for entry in (_load(root, "semantic_scope.yaml").get("scope") or [])
        }
        aliases = _load(root, "concept_aliases.yaml").get("concepts") or []
        return cls(scope, aliases)

    def concept_of(self, policy_id: str) -> str | None:
        entry = self.scope.get(policy_id)
        return entry.get("concept_id") if entry else None

    def resolve_concepts(
        self,
        question: str,
        topics: list[str],
        store: Any,
        ranked_policy_ids: list[str] | None = None,
        agreement_window: int = 5,
    ) -> set[str]:
        """The concepts the QUESTION is about, given the topics it resolved to.

        For a topic holding one concept the topic settles it. For the five holding
        two, a curated alias decides; if none matches, BOTH are returned — an
        ambiguous question must not silently pick one of two regulatory
        institutions, and leaving both in place downgrades neither to direct.
        """
        from core.services.policy_store import expand_tokens

        if not topics:
            # Curated topic aliases fire on roughly a quarter of questions; the rest
            # reach their records lexically. Requiring a topic left 57% of questions
            # with a citable rule unable to state one, which is not caution, it is
            # breakage. So fall back to the concept of the strongest retrieved
            # records — but ONLY when they AGREE. Agreement is the actual signal: a
            # question the store governs pulls records of one concept, while
            # «كم مرة أعيد نفس المادة؟» — which the store does not govern — pulls
            # GPA, re-enrolment, transfer and cheating records at once, and that
            # disagreement is exactly what must yield no direct evidence.
            import collections

            head = [
                c
                for c in (self.concept_of(p) for p in (ranked_policy_ids or [])[:agreement_window])
                if c
            ]
            counted = collections.Counter(head)
            if not counted:
                return set()
            concept, hits = counted.most_common(1)[0]
            # Two independent records of the same concept in the top five. Chosen by
            # measurement, not taste: requiring the top TWO to agree left only 68% of
            # EXPLAIN_ONLY questions able to state a rule that exists, while this
            # reaches 82% and still blocks both live failures. Requiring only the top
            # record reaches 97% and blocks neither — the agreement IS the signal.
            return {concept} if hits >= 2 else set()
        by_topic: dict[str, set[str]] = {}
        for policy_id, entry in self.scope.items():
            record = store.by_id.get(policy_id)
            if record:
                by_topic.setdefault(record["topic"], set()).add(entry["concept_id"])

        q_tokens = expand_tokens(question)
        resolved: set[str] = set()
        for topic in topics:
            candidates = by_topic.get(topic, set())
            if len(candidates) <= 1:
                resolved |= candidates
                continue
            matched = set()
            for entry in self.concept_aliases:
                if entry["topic"] != topic:
                    continue
                for alias in entry.get("aliases_ar") or []:
                    words = [expand_tokens(w) for w in alias.split() if expand_tokens(w)]
                    if words and all(variants & q_tokens for variants in words):
                        matched.add(entry["concept_id"])
                        break
            resolved |= matched or candidates
        return resolved


_index: ApplicabilityIndex | None = None


def get_applicability_index(
    root: Path | None = None, *, refresh: bool = False
) -> ApplicabilityIndex:
    global _index
    if _index is None or refresh or root is not None:
        from core.services.policy_store import policy_root

        _index = ApplicabilityIndex.load(root or policy_root())
    return _index


def classify(
    policies: list[dict[str, Any]],
    *,
    question: str,
    topics: list[str],
    store: Any,
    index: ApplicabilityIndex | None = None,
) -> dict[str, Any]:
    """Sort retrieved policies into roles. Returns the three collections plus a trace.

    The default is BACKGROUND, not DIRECT. A record has to earn the right to ground a
    rule; failing to classify one is not a reason to trust it.
    """
    index = index or get_applicability_index()
    question_concepts = index.resolve_concepts(
        question, topics, store, [p.get("policy_id") for p in policies]
    )

    direct: list[dict[str, Any]] = []
    background: list[dict[str, Any]] = []
    conflicting: list[dict[str, Any]] = []
    irrelevant: list[dict[str, Any]] = []

    for policy in policies:
        policy_id = policy.get("policy_id")
        entry = index.scope.get(policy_id) or {}
        concept = entry.get("concept_id")
        annotated = {
            **policy,
            "concept_id": concept,
            "governing_entity": entry.get("governing_entity"),
            "action": entry.get("action"),
            "claim_types": entry.get("claim_types") or [],
        }

        # A record superseded by a higher authority never grounds a claim, whatever
        # its concept: the store already knows a different source governs.
        if policy.get("conflicts") and any(not c.get("governs") for c in policy["conflicts"]):
            annotated["role"] = CONFLICTING
            annotated["role_reason"] = "superseded by a higher-authority source on this subject"
            conflicting.append(annotated)
            continue

        if concept and question_concepts and concept in question_concepts:
            annotated["role"] = DIRECT_SUPPORT
            annotated["role_reason"] = f"governs {concept}, which is what the question asks about"
            direct.append(annotated)
            continue

        # Unscoped or off-concept. Explicitly excluded is worth naming separately so
        # an operator can see the block was deliberate rather than incidental.
        excluded = set(entry.get("excluded_intents") or [])
        if concept and question_concepts and excluded:
            annotated["role"] = IRRELEVANT
            annotated["role_reason"] = (
                f"governs {concept}; the question is about {', '.join(sorted(question_concepts))}"
            )
            irrelevant.append(annotated)
            continue

        annotated["role"] = BACKGROUND_ONLY
        annotated["role_reason"] = (
            "related but not shown to govern this question — may be mentioned as "
            "existing, never used to derive a limit, eligibility or deadline"
        )
        background.append(annotated)

    return {
        "question_concepts": sorted(question_concepts),
        "direct_policy_evidence": direct,
        "background_policy_evidence": background,
        "conflicting_policy_evidence": conflicting,
        "irrelevant_policy_evidence": irrelevant,
        "direct_count": len(direct),
    }

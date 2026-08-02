"""What travels with a case when a student hands it to a human adviser.

Two rules shape this module, and both are about what is NOT here.

**The snapshot is an allowlist, built field by field.** Not the agent result with
the dangerous keys removed. A denylist over a dict that another module owns leaks
the moment that module gains a key, and the agent result carries tool output,
retrieval state and judge findings — material that names database tables, quotes
cohort statistics and describes the system's opinion of its own answer. The
adviser needs the question, the answer, its sources, the facts about this student
that bear on it, and what was missing. Nothing else is theirs to receive.

**It is a frozen copy, and it is built from student-visible messages.** An
escalation is a record of what was said at the time. Rebuilt later from live
policies and a live student record, a case would quietly change its own facts
between being raised and being read, and the adviser would answer a question the
student never asked.
"""

from __future__ import annotations

from typing import Any

#: Every key the snapshot may contain. The builder returns exactly these, and the
#: validator refuses anything else — so adding a field is a deliberate edit here
#: rather than something that happens by accident upstream.
EVIDENCE_FIELDS: tuple[str, ...] = (
    "question",
    "assistant_answer",
    "answer_mode",
    "final_disposition",
    "reason_codes",
    "relevant_student_facts",
    "citations",
    "missing_information",
)

#: Per-citation. The same six the student sees — an adviser checking a reference
#: needs the reference, not the store's internal record.
CITATION_FIELDS: tuple[str, ...] = (
    "policy_id",
    "document_title",
    "edition",
    "page",
    "effective_from",
    "effective_to",
)

#: The only student attributes that may appear. Deliberately short: an escalation
#: about a withdrawal does not need a home address, and "everything we know" is
#: how an adviser ends up reading a record they had no reason to open.
STUDENT_FACT_FIELDS: tuple[str, ...] = (
    "student_id",
    "name",
    "program",
    "status",
    "gpa",
    "total_earned_credits",
    "current_registered_credits",
)


class EvidenceError(ValueError):
    """The snapshot contains something no adviser asked for."""


def validate_evidence(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Refuse a snapshot that strays outside the allowlist.

    Called on the way IN, not on the way out. A snapshot is written once and read
    many times; catching a stray field at read time means it was already stored,
    already backed up, and possibly already read by someone.
    """
    unknown = set(snapshot) - set(EVIDENCE_FIELDS)
    if unknown:
        raise EvidenceError(f"evidence carries fields no adviser asked for: {sorted(unknown)}")

    facts = snapshot.get("relevant_student_facts") or {}
    stray_facts = set(facts) - set(STUDENT_FACT_FIELDS)
    if stray_facts:
        raise EvidenceError(f"student facts outside the allowlist: {sorted(stray_facts)}")

    for citation in snapshot.get("citations") or []:
        stray = set(citation) - set(CITATION_FIELDS)
        if stray:
            raise EvidenceError(f"citation carries extra fields: {sorted(stray)}")
    return snapshot


def deterministic_summary(evidence: dict[str, Any]) -> str:
    """A summary written without a model.

    The generated summary is a convenience for the adviser, and a convenience must
    not be able to block a case. If the model is down — which is frequently WHY a
    turn ended up escalated — an escalation that failed to save would lose the
    student the one route they had to a human. So this is the floor: assembled
    from the persisted fields, always available, and never wrong in a way the
    fields are not already wrong.
    """
    question = str(evidence.get("question") or "").strip()
    answer = str(evidence.get("assistant_answer") or "").strip()
    reasons = evidence.get("reason_codes") or []
    missing = evidence.get("missing_information") or []
    citations = evidence.get("citations") or []

    lines = [f"سؤال الطالب: {question}" if question else "سؤال الطالب: (غير متوفر)"]
    if reasons:
        lines.append("سبب الإحالة: " + "، ".join(str(r) for r in reasons))
    if missing:
        lines.append("معلومات ناقصة: " + "، ".join(str(m) for m in missing))
    if citations:
        refs = "؛ ".join(
            " ".join(
                part
                for part in (
                    str(c.get("document_title") or ""),
                    f"ص {c.get('page')}" if c.get("page") else "",
                    str(c.get("policy_id") or ""),
                )
                if part
            )
            for c in citations
        )
        lines.append("المراجع المستشهد بها: " + refs)
    else:
        lines.append("المراجع المستشهد بها: لا توجد.")
    if answer:
        # Bounded: the adviser has the full answer in the snapshot, and a summary
        # that reproduces it is not a summary.
        lines.append("خلاصة رد النظام: " + (answer[:600] + "…" if len(answer) > 600 else answer))
    return "\n".join(lines)

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

from core.services.advisor_outcome import (
    OutcomeError,
    validate_missing_information,
    validate_reason_codes,
)

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

    # Delegated rather than re-implemented: the turn and the case must agree about
    # what a missing-information entry is, or a snapshot could carry a shape the
    # message itself would have refused.
    try:
        validate_reason_codes(snapshot.get("reason_codes") or [])
        validate_missing_information(snapshot.get("missing_information") or [])
    except OutcomeError as exc:
        raise EvidenceError(str(exc)) from exc
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
        # The Arabic label, never the code: the summary is read by a person, and
        # WITHDRAWAL_HISTORY is the vocabulary the queue sorts on, not a sentence.
        lines.append(
            "معلومات ناقصة: "
            + "، ".join(str(m.get("label_ar") or m.get("code") or "") for m in missing)
        )
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


# ── building a case from committed rows ──────────────────────────

#: Which reason to file the case under when the student did not ask for a person
#: outright. Ordered by how much a human is actually needed: a decision the
#: regulation reserves to a person outranks a rule nobody could find, which
#: outranks an outage. The first match wins, so the queue sorts on one stable
#: value rather than on whichever reason happened to be appended first.
_ESCALATION_PRIORITY = (
    "PROHIBITED_FOR_DECISION",
    "CONFLICTING_AUTHORITIES",
    "JUDGE_REJECTED",
    # Beside JUDGE_REJECTED because it is the same kind of fact: the answer was
    # produced and then found unfit. It outranks the "nothing was found" codes,
    # which describe an absent source rather than a defective answer.
    "OUTPUT_NOT_GROUNDED",
    "STUDENT_DATA_MISSING",
    "PROCEDURE_NOT_DOCUMENTED",
    "POLICY_NOT_FOUND",
    "POLICY_UNAVAILABLE",
    "MODEL_UNAVAILABLE",
)


def may_escalate(message: Any, *, student_requested: bool = False) -> bool:
    """Whether this turn can be handed to a person.

    A student may ask for a human about a perfectly good answer — being satisfied
    with a system's reply is not a precondition for wanting a person to look at it
    — so `student_requested` opens the door on its own.

    What is deliberately NOT here: automatic escalation of every turn that found no
    governing policy. Plenty of unsupported questions are about services outside
    academic advising and need redirecting, not a case in an adviser's queue.
    """
    if student_requested:
        return True
    disposition = str(getattr(message, "final_disposition", "") or "")
    if disposition in {"ABSTAIN", "ESCALATE"}:
        return True
    return "PROHIBITED_FOR_DECISION" in (getattr(message, "reason_codes", None) or [])


def escalation_reason(message: Any, *, student_requested: bool = False) -> str:
    """Why a PERSON was asked — which is not always why the answer was limited.

    A student requesting review of an answer that passed is STUDENT_REQUESTED here
    while the turn itself keeps its own reasons, which may be none. Collapsing the
    two would rewrite the record of what constrained the answer every time somebody
    pressed a button.
    """
    if student_requested:
        return "STUDENT_REQUESTED"
    codes = list(getattr(message, "reason_codes", None) or [])
    for candidate in _ESCALATION_PRIORITY:
        if candidate in codes:
            return candidate
    return "STUDENT_REQUESTED"


def build_evidence(message: Any) -> dict[str, Any]:
    """Freeze what the adviser will read, from the stored turn and nothing else.

    Every field comes from a committed row: the question from the student message
    this one replies to, the answer and typed outcome from the message itself, the
    references from its citation snapshots. Nothing is looked up live.

    That is the whole point. A case opened tomorrow against today's answer must
    show today's evidence — if the policy store or the student's record is consulted
    at creation time, the adviser reads a case whose facts have moved since the
    student was given them, and neither of them can tell.

    `relevant_student_facts` is `{}` until the turn persists them. Reconstructing
    them from the live record would reintroduce exactly the drift above, and
    reading them out of the Arabic answer would invent structure from prose.
    """
    question = getattr(message.in_reply_to, "content", "") if message.in_reply_to_id else ""
    snapshot = {
        "question": str(question or ""),
        "assistant_answer": str(message.content or ""),
        "answer_mode": str(message.answer_mode or ""),
        "final_disposition": str(message.final_disposition or ""),
        "reason_codes": list(message.reason_codes or []),
        "relevant_student_facts": {},
        "citations": [
            {
                "policy_id": c.policy_id,
                "document_title": c.document_title,
                "edition": c.edition,
                "page": c.page,
                "effective_from": c.effective_from,
                "effective_to": c.effective_to,
            }
            for c in message.citations.all()
        ],
        "missing_information": list(message.missing_information or []),
    }
    return validate_evidence(snapshot)

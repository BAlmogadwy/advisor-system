"""What the final answer actually did, as a typed value the escalation layer can trust.

Two ideas hold this module together.

**The disposition describes the response, not the inputs.** A policy marked
`PROHIBITED_FOR_DECISION` says the university will not have this adjudicated
automatically — it does not say the turn failed. Asked "وش معنى الإنذار
الأكاديمي؟", the adviser can explain the status perfectly well from that same
record; only *applying* it to this student is forbidden. Deriving the disposition
from the policy would mark that explanation as an abstention and, once escalation
is wired, offer a human hand-off to a student who was simply told what a word
means. So the constraint shapes the answer, and the answer decides the outcome.

**It is derived last, from the version the student will see.** After the citation
check, after the grounding retry, after any judge. Persisting a preliminary
disposition and then correcting it leaves a window in which the stored outcome
disagrees with the stored answer, and the escalation layer reads the stored
outcome. Everything here is computed once, from the final result, and written in
the same transaction as the message and its citations.

Four of the nine reason codes have no producer yet — the judge is not wired into
the request path, and the runtime emits no structured account of what it lacked.
They are defined anyway, because the vocabulary is the contract the escalation
layer consumes; a code that cannot yet fire is honest, whereas a code invented at
read time from Arabic prose is not.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: Bumped when the meaning of a stored outcome changes, so a reader can tell a row
#: written under different rules from one it can interpret.
OUTCOME_SCHEMA_VERSION = "1.1"


class ReasonCode:
    """Why the answer stopped where it did. Server-set, never client-supplied."""

    PROHIBITED_FOR_DECISION = "PROHIBITED_FOR_DECISION"
    POLICY_NOT_FOUND = "POLICY_NOT_FOUND"
    POLICY_UNAVAILABLE = "POLICY_UNAVAILABLE"
    STUDENT_DATA_MISSING = "STUDENT_DATA_MISSING"
    PROCEDURE_NOT_DOCUMENTED = "PROCEDURE_NOT_DOCUMENTED"
    CONFLICTING_AUTHORITIES = "CONFLICTING_AUTHORITIES"
    JUDGE_REJECTED = "JUDGE_REJECTED"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    STUDENT_REQUESTED = "STUDENT_REQUESTED"
    #: The answer was withheld because it named identifiers the system could
    #: not support. A sibling of a failed citation check, and it must be
    #: reported for the same reason: a turn that ends in a refusal has not
    #: answered the student, whatever the prose looks like.
    OUTPUT_NOT_GROUNDED = "OUTPUT_NOT_GROUNDED"
    #: The typed semantic planner correctly identified a deliverable that the
    #: read-only adviser cannot provide (including a requested portal mutation).
    CAPABILITY_UNSUPPORTED = "CAPABILITY_UNSUPPORTED"
    #: A schema-valid plan omitted a requested deliverable or selected unrelated
    #: evidence. No capability was executed under that incomplete contract.
    SEMANTIC_PLAN_INCOMPLETE = "SEMANTIC_PLAN_INCOMPLETE"

    ALL = frozenset(
        {
            PROHIBITED_FOR_DECISION,
            POLICY_NOT_FOUND,
            POLICY_UNAVAILABLE,
            STUDENT_DATA_MISSING,
            PROCEDURE_NOT_DOCUMENTED,
            CONFLICTING_AUTHORITIES,
            JUDGE_REJECTED,
            MODEL_UNAVAILABLE,
            STUDENT_REQUESTED,
            OUTPUT_NOT_GROUNDED,
            CAPABILITY_UNSUPPORTED,
            SEMANTIC_PLAN_INCOMPLETE,
        }
    )


#: What the runtime is allowed to say it was missing. A closed set for the same
#: reason the reason codes are closed: an adviser triages on these, and a free-text
#: code is a category nobody can count.
MISSING_INFORMATION_CODES = frozenset(
    {
        "WITHDRAWAL_HISTORY",
        "CURRENT_CALENDAR_WINDOW",
        "REGISTRATION_STATUS",
        "TRANSCRIPT_DETAIL",
        "PROBATION_HISTORY",
        "PROGRAMME_PLAN_VERSION",
        "ADVISER_APPROVAL",
        "PAYMENT_STATUS",
    }
)

MAX_MISSING_ITEMS = 10
MAX_LABEL_CHARS = 120

#: Any Arabic letter. A label is shown to a student and to an adviser, so it must
#: actually be in the language they read — an English code echoed into the label
#: field is a leak of the internal vocabulary dressed as a description.
_ARABIC = tuple(range(0x0600, 0x0700))


class OutcomeError(ValueError):
    """The runtime tried to persist an outcome that is not in the contract."""


@dataclass(frozen=True)
class Outcome:
    disposition: str
    reason_codes: list[str] = field(default_factory=list)
    missing_information: list[dict[str, str]] = field(default_factory=list)
    schema_version: str = OUTCOME_SCHEMA_VERSION


def validate_reason_codes(codes: Any) -> list[str]:
    """Refuse anything outside the vocabulary, on the way in.

    Order-preserving and de-duplicated: the first code is the one an adviser queue
    sorts on, so it must be stable, and the same reason twice is noise.
    """
    if not isinstance(codes, list):
        raise OutcomeError("reason_codes must be a list")
    seen: list[str] = []
    for code in codes:
        if code not in ReasonCode.ALL:
            raise OutcomeError(f"unknown reason code: {code!r}")
        if code not in seen:
            seen.append(code)
    return seen


def validate_missing_information(entries: Any) -> list[dict[str, str]]:
    """Accept only structured, bounded, Arabic-labelled entries.

    Never derived from the answer text. Parsing prose for "what was missing"
    invents a machine-readable field out of a sentence written for a human, and
    the escalation layer would then be triaging on a guess.
    """
    if not isinstance(entries, list):
        raise OutcomeError("missing_information must be a list")
    if len(entries) > MAX_MISSING_ITEMS:
        raise OutcomeError(f"missing_information holds more than {MAX_MISSING_ITEMS} entries")

    out: list[dict[str, str]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise OutcomeError("each missing_information entry must be an object")
        if set(entry) != {"code", "label_ar"}:
            raise OutcomeError(f"entry must carry exactly code and label_ar: {sorted(entry)}")

        code = entry["code"]
        if code not in MISSING_INFORMATION_CODES:
            raise OutcomeError(f"unknown missing_information code: {code!r}")

        label = entry["label_ar"]
        if not isinstance(label, str) or not label.strip():
            raise OutcomeError(f"{code} carries no label")
        if len(label) > MAX_LABEL_CHARS:
            raise OutcomeError(f"{code} label is longer than {MAX_LABEL_CHARS} characters")
        if not any(ord(ch) in _ARABIC for ch in label):
            raise OutcomeError(f"{code} label is not in Arabic: {label!r}")
        if any(ch.isdigit() for ch in label):
            # A label names a KIND of missing fact. A number in it is this student's
            # own datum, which belongs in the record, not in a category name.
            raise OutcomeError(f"{code} label carries a value rather than a description")

        out.append({"code": code, "label_ar": label.strip()})

    codes = [e["code"] for e in out]
    if len(codes) != len(set(codes)):
        raise OutcomeError("missing_information repeats a code")
    return out


# ── deriving the outcome from the final result ───────────────────

#: Capabilities that read THIS student's own record. Their presence is what makes a
#: turn personal: a question answered purely from the policy store is about the
#: rule, and a question that also opened the student's file is about them.
_STUDENT_SCOPED_TOOLS = frozenset(
    {
        "my_progress",
        "why_course_locked",
        "graduation_progress",
        "my_timetable",
        "student_profile",
        "course_eligibility",
        "recommend_next_courses",
        "current_registrations",
        "remaining_requirements",
        "recommend_feasible_course_addition",
        "rank_current_course_drop_impact",
        "improve_current_timetable",
    }
)


def _policy_tool_results(agent: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        r
        for r in (agent.get("tool_results") or [])
        if isinstance(r, dict) and r.get("tool") == "policy_lookup"
    ]


def prohibited_policy_ids(agent: dict[str, Any]) -> set[str]:
    """Direct evidence the university forbids applying to an individual.

    Read from `direct_policy_evidence`, never from `policies`: a background record
    that happens to be undecidable says nothing about the question that was asked,
    and counting it would attach the constraint to turns it does not govern.
    """
    out: set[str] = set()
    for result in _policy_tool_results(agent):
        for record in result.get("direct_policy_evidence") or []:
            if not isinstance(record, dict):
                continue
            if str(record.get("decision_use") or "") == ReasonCode.PROHIBITED_FOR_DECISION:
                policy_id = str(record.get("policy_id") or "")
                if policy_id:
                    out.add(policy_id)
    return out


def _asked_for_a_personal_decision(result: dict[str, Any], agent: dict[str, Any]) -> bool:
    """Did the answer apply a prohibited rule to THIS student?

    Approximate, and deliberately so. The exact signal is claim-level — "this
    sentence is a normative claim about this student" — and the runtime does not
    yet emit claims as structured output. Until it does, the honest proxy is that
    the answer both CITED the prohibited record and had the student's own file
    open: either alone is a general explanation.

    The consequence of the approximation is stated plainly: a turn that opened the
    student's record for an unrelated part of the question and cited the prohibited
    rule for a general part will be treated as personal. That errs towards offering
    a human, which is the safe direction.
    """
    prohibited = prohibited_policy_ids(agent)
    if not prohibited:
        return False
    if not (prohibited & set(result.get("cited_policy_ids") or [])):
        return False
    called = {
        str(c.get("tool") or "") for c in (agent.get("tools_called") or []) if isinstance(c, dict)
    }
    called |= {
        str(r.get("tool") or "") for r in (agent.get("tool_results") or []) if isinstance(r, dict)
    }
    return bool(called & _STUDENT_SCOPED_TOOLS)


def derive_outcome(
    result: dict[str, Any],
    *,
    escalated: bool = False,
    student_requested: bool = False,
) -> Outcome:
    """Read the FINAL result and say what it did.

    Call once, after the citation check and any retry, with the answer the student
    will actually see. `escalated` is for the product flow that creates a case;
    `student_requested` for a student who asked for a person outright.
    """
    agent = result.get("agent") or {}
    reasons: list[str] = []

    # ── the infrastructure ──────────────────────────────────────
    if agent.get("turn_error"):
        # No safe answer could be produced because something below the adviser
        # broke. Distinct from abstaining, which is a decision the adviser made.
        return Outcome(
            disposition="FAILED",
            reason_codes=[ReasonCode.MODEL_UNAVAILABLE],
            missing_information=[],
        )

    # GATED ON `policy_required`, and it has to be. Retrieval is now unconditional
    # and server-side, so `policy_grounding` is a real state for every turn —
    # including «وين قاعة GS112؟», where the store legitimately holds nothing
    # governing. Before the prefetch such a turn recorded `not_consulted` and
    # produced no reason code; ungated, the same turn would now record
    # `none_matched` -> POLICY_NOT_FOUND -> ABSTAIN. Measured over the
    # 284-question corpus that inverts the disposition of 83 questions, none of
    # which asked for a rule. A missing rule is only a reason when a rule was owed.
    #
    # Defaults to True for a payload that predates the flag: an older stored turn
    # keeps the behaviour it was written under.
    grounding = str(agent.get("policy_grounding") or "")
    policy_required = bool(agent.get("policy_required", True))
    if policy_required:
        if grounding == "unavailable":
            reasons.append(ReasonCode.POLICY_UNAVAILABLE)
        elif grounding in {"none_matched", "none_governing"}:
            reasons.append(ReasonCode.POLICY_NOT_FOUND)

    # Also gated. Unconditional retrieval surfaces conflicting records on 23 of the
    # 284 corpus questions, and CONFLICTING_AUTHORITIES escalates unconditionally a
    # few lines below — so ungating it would open a real adviser case because a
    # timetable question's retrieval happened to touch two disagreeing records.
    if policy_required and any(
        r.get("conflicting_policy_evidence") for r in _policy_tool_results(agent)
    ):
        reasons.append(ReasonCode.CONFLICTING_AUTHORITIES)

    if _asked_for_a_personal_decision(result, agent):
        reasons.append(ReasonCode.PROHIBITED_FOR_DECISION)

    missing = validate_missing_information(result.get("missing_information") or [])
    if missing:
        reasons.append(ReasonCode.STUDENT_DATA_MISSING)

    if agent.get("grounding_refused"):
        # The output contract replaced the answer. Without this the turn persists
        # as COMPLETED with a refusal in the body: the UI shows no status note,
        # and `may_escalate` refuses the student a human with «هذه الإجابة لا
        # تحتاج إلى مراجعة» — a refused answer presented as a resolved one.
        reasons.append(ReasonCode.OUTPUT_NOT_GROUNDED)

    semantic_outcomes = {
        str(value or "") for value in (agent.get("semantic_plan_requested_outcomes") or [])
    }
    if (
        agent.get("semantic_plan_decision") == "unsupported"
        or "registration_action" in semantic_outcomes
        or "credit_load_comparison" in semantic_outcomes
    ):
        reasons.append(ReasonCode.CAPABILITY_UNSUPPORTED)

    if agent.get("semantic_outcome_coverage_refused"):
        reasons.append(ReasonCode.SEMANTIC_PLAN_INCOMPLETE)

    if agent.get("judge_action") == "ESCALATE":
        reasons.append(ReasonCode.JUDGE_REJECTED)

    if student_requested:
        reasons.append(ReasonCode.STUDENT_REQUESTED)

    # ── what the response ended up being ────────────────────────
    if (
        escalated
        or student_requested
        or ReasonCode.JUDGE_REJECTED in reasons
        or ReasonCode.CONFLICTING_AUTHORITIES in reasons
    ):
        disposition = "ESCALATE"
    elif (
        agent.get("citation_refused")
        or agent.get("grounding_refused")
        or ReasonCode.CAPABILITY_UNSUPPORTED in reasons
        or ReasonCode.SEMANTIC_PLAN_INCOMPLETE in reasons
    ):
        # The answer was withheld — because its citations could not be verified,
        # or because it named identifiers the evidence does not support. Both are
        # the system declining to answer, not answering.
        disposition = "ABSTAIN"
    elif (
        ReasonCode.PROHIBITED_FOR_DECISION in reasons or ReasonCode.STUDENT_DATA_MISSING in reasons
    ):
        # A personal decision the university reserves to a human, or facts the
        # system does not hold. Either way the request was not resolved — but it is
        # only ESCALATE once a case actually exists, which is the branch above.
        disposition = "ABSTAIN"
    elif ReasonCode.POLICY_NOT_FOUND in reasons and not (result.get("cited_policy_ids") or []):
        # Nothing governing was found AND nothing was cited: there is no grounded
        # answer here. With a citation present the turn answered something, so the
        # missing rule is a caveat rather than the outcome.
        disposition = "ABSTAIN"
    else:
        disposition = "PASS"

    return Outcome(
        disposition=disposition,
        reason_codes=validate_reason_codes(reasons),
        missing_information=missing,
    )

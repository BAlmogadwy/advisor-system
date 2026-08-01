"""Independent certification of a board against a snapshot.

Tri-state, deliberately:

* ``VALID``       — every graded rule passed, and nothing was left ungraded.
* ``INVALID``     — a hard rule was violated.
* ``UNCERTIFIED`` — nothing was violated, but something could not be *looked at*
  (missing evidence, missing implementation).

The third state is the whole point. "No violation found" and "not looked for" are
different claims, and a checker that conflates them is worse than no checker —
it manufactures confidence. The old engine reported a bare boolean, so a board
that had never been checked for student overlap read the same as one that had.

Unroomed physical meetings are reported but do **not** invalidate (D7): a room is
an assignment that can be left unmade. They do block *publication*, which is a
separate decision made later against this report.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from scheduler.domain import Snapshot
from scheduler.domain.board import Board
from scheduler.rules import (
    DECLARED_GAPS,
    RULEBOOK,
    Enforcement,
    RuleResult,
    Severity,
    check_rule,
    rulebook_fingerprint,
)


class Certification(str, Enum):
    VALID = "VALID"
    INVALID = "INVALID"
    UNCERTIFIED = "UNCERTIFIED"


@dataclass
class ValidationReport:
    snapshot_fingerprint: str
    rulebook_fingerprint: str
    board_summary: dict
    results: list[RuleResult] = field(default_factory=list)

    @property
    def violated(self) -> list[RuleResult]:
        return [r for r in self.results if r.severity is Severity.HARD and r.violations]

    @property
    def ungraded(self) -> list[RuleResult]:
        return [
            r
            for r in self.results
            if r.enforcement in (Enforcement.EVIDENCE_GAP, Enforcement.COVERAGE_GAP)
        ]

    @property
    def certification(self) -> Certification:
        if self.violated:
            return Certification.INVALID
        if self.ungraded:
            return Certification.UNCERTIFIED
        return Certification.VALID

    @property
    def violation_count(self) -> int:
        return sum(len(r.violations) for r in self.violated)

    def as_dict(self) -> dict:
        return {
            "certification": self.certification.value,
            "violations": self.violation_count,
            "board": self.board_summary,
            "fingerprints": {
                "snapshot": self.snapshot_fingerprint,
                "rulebook": self.rulebook_fingerprint,
            },
            "rules": [r.as_dict() for r in self.results],
        }


def validate(snapshot: Snapshot, board: Board) -> ValidationReport:
    """Grade a board. Reads only the snapshot and the board — no solver state.

    Independence is the point: this shares the constraint *declarations* with any
    future solver, but none of its incremental occupancy caches or delta logic. A
    disagreement between the two is therefore a real finding rather than the same
    code agreeing with itself.
    """
    report = ValidationReport(
        snapshot_fingerprint=snapshot.fingerprint(),
        rulebook_fingerprint=rulebook_fingerprint(),
        board_summary=board.summary(),
    )
    for spec in RULEBOOK:
        report.results.append(check_rule(spec, snapshot, board))

    # Rules the institution declares that we cannot grade here — listed with the
    # reason, never silently omitted.
    for rule_id, title, mode, why in DECLARED_GAPS:
        report.results.append(RuleResult(rule_id, title, mode, Severity.HARD, note=why))

    # Observability: unroomed physical meetings. Not a violation (D7).
    unroomed = board.unroomed
    report.results.append(
        RuleResult(
            "H_ROOM_REQUIRED",
            "Physical meeting has a room",
            Enforcement.CHECK,
            Severity.OBSERVED,
            violations=(),
            note=(
                f"{len(unroomed)} of {len(board.physical)} physical meetings have no "
                f"room assigned. Legal during building; blocks publication."
            ),
        )
    )
    return report

"""The rulebook — one declaration per constraint, projected into every path.

The design problem this solves is the one that made the old engine drift: a class
holding `check`, `check_delta` and `add_to_cpsat` is **still three executable
definitions**. They can and did diverge — instructor clash had eight
implementations, one of which compared start times while the others compared
intervals, so a 10:30-11:45 lecture and a 10:45-12:25 lab conflicted in some
stages and not others.

So a rule here is *declared*, not coded: a frozen dataclass naming what to group
by and what to compare. One declaration is then projected into whole-board
checking today, and into delta checking and CP-SAT compilation later, with no
second chance to disagree. There are no executable selectors (no lambdas), which
also makes the whole rulebook hashable — the declaration fingerprint is what
tells you whether two runs were even judged by the same rules.

Enforcement modes are declared, never blank. A rule that cannot be checked yet
says so (`EVIDENCE_GAP` / `COVERAGE_GAP`) rather than silently passing — the
difference between "no violation found" and "not looked for" is the whole point
of an independent checker.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import Enum

from scheduler.domain import Snapshot
from scheduler.domain.board import Board, Placement


class Severity(str, Enum):
    HARD = "hard"  # a violation makes the board invalid
    OBSERVED = "observed"  # reported, does not invalidate (e.g. unroomed, D7)


class Enforcement(str, Enum):
    """Where a rule is enforced. No blank cells, ever."""

    CHECK = "check"  # graded by the independent checker
    NOT_APPLICABLE = "not_applicable"  # structurally impossible here
    EVIDENCE_GAP = "evidence_gap"  # checkable, but its inputs do not exist yet
    COVERAGE_GAP = "coverage_gap"  # declared, implementation still owed


@dataclass(frozen=True)
class Violation:
    rule_id: str
    message: str
    witnesses: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {"rule": self.rule_id, "message": self.message, "witnesses": list(self.witnesses)}


@dataclass(frozen=True)
class RuleResult:
    rule_id: str
    title: str
    enforcement: Enforcement
    severity: Severity
    violations: tuple[Violation, ...] = ()
    note: str = ""

    @property
    def passed(self) -> bool:
        return self.enforcement is Enforcement.CHECK and not self.violations

    @property
    def graded(self) -> bool:
        return self.enforcement is Enforcement.CHECK

    def as_dict(self) -> dict:
        return {
            "rule": self.rule_id,
            "title": self.title,
            "enforcement": self.enforcement.value,
            "severity": self.severity.value,
            "violations": [v.as_dict() for v in self.violations],
            "note": self.note,
        }


# ── the declarative rule types ────────────────────────────────────────────
#
# Each names a grouping and a comparison. None contains scheduling logic beyond
# that, which is what keeps a single declaration projectable into several paths.


@dataclass(frozen=True)
class RuleSpec:
    rule_id: str
    title: str
    severity: Severity = Severity.HARD

    def declaration(self) -> dict:
        """The fingerprintable form. Subclasses add their parameters."""
        return {"rule": self.rule_id, "type": type(self).__name__, "severity": self.severity.value}


@dataclass(frozen=True)
class NoOverlapByKey(RuleSpec):
    """No two placements sharing a key may overlap in time.

    One declaration serves instructor clash (key = instructor), room exclusivity
    (key = room) and same-offering separation (key = offering). Those were three
    separately-written rules in the old engine, and they drifted apart.
    """

    key: str = ""  # "instructor" | "room" | "offering"
    scope: str = "scenario"
    #: Offerings whose sections are REQUIRED to share hours are exempt from the
    #: same-offering separation (D19: ENG101/ENG102, where the owner's rule is
    #: that every section sits at the same time). Declared here rather than
    #: buried in the checker, so the exemption is fingerprinted with the rule and
    #: appears in the certification, instead of being a silent kindness the
    #: grader extends to the solver that produced the board.
    #:
    #: Applies to the "offering" key only. Instructor clash and room exclusivity
    #: are physical and are never exempted for anybody.
    exempt_fixed_blocks: bool = True

    def keys_of(self, p: Placement) -> tuple[str, ...]:
        if self.key == "instructor":
            return (str(p.instructor_id),) if p.instructor_id is not None else ()
        if self.key == "room":
            return (str(p.room_id),) if p.room_id is not None else ()
        if self.key == "offering":
            return (str(p.offering_id),)
        raise ValueError(f"unknown key {self.key!r}")

    def declaration(self) -> dict:
        out = {**super().declaration(), "key": self.key, "scope": self.scope}
        if self.key == "offering":
            out["exempt_fixed_blocks"] = self.exempt_fixed_blocks
        return out


@dataclass(frozen=True)
class DistinctDays(RuleSpec):
    """All meetings of one group fall on different days.

    A course whose hours are GIVEN rather than chosen (D19 — ENG101/ENG102 own
    the whole morning) meets more than once a day by design. For those sections
    the rule is not dropped, it is REFINED to the finer key it was always
    standing in for: no two meetings in the same CELL. Dropping it outright
    would let a section be booked twice at 09:00 on Sunday with nothing to say
    so, and a checker that stops checking is how a checker starts lying.
    """

    group: str = "section"
    #: When True, sections of a fixed-block offering are graded on (day, start)
    #: instead of on day alone.
    refine_for_fixed_blocks: bool = True

    def declaration(self) -> dict:
        return {
            **super().declaration(),
            "group": self.group,
            "refine_for_fixed_blocks": self.refine_for_fixed_blocks,
        }


@dataclass(frozen=True)
class DailyCardinality(RuleSpec):
    """A key may appear at most `cap` times on one day.

    Counts *meeting presences*, not one boolean per section — a section teaching
    twice on a day consumes two of the cap.
    """

    key: str = "instructor"
    cap: int = 3

    def declaration(self) -> dict:
        return {**super().declaration(), "key": self.key, "cap": self.cap}


@dataclass(frozen=True)
class LegalWindow(RuleSpec):
    """A meeting occupies a window the grid declares for its **duration**.

    Timing family follows duration; room family follows kind (D6). A 100-minute
    lecture is legal at the 100-minute windows while needing a lecture room.
    """

    def declaration(self) -> dict:
        return {**super().declaration(), "keyed_on": "duration"}


@dataclass(frozen=True)
class ExactMeetingMultiset(RuleSpec):
    """A section's placed meetings equal its offering's required multiset."""

    def declaration(self) -> dict:
        return {**super().declaration(), "compares": "kind+delivery+duration+count"}


@dataclass(frozen=True)
class RoomCompatibility(RuleSpec):
    """An assigned room must satisfy every declared attribute of the meeting.

    Gender is absent by construction: a snapshot is single-gender (D1), so the
    room pool was filtered at intake and the rule cannot fail.
    """

    check_type: bool = True
    check_capacity: bool = True
    check_programme: bool = True

    def declaration(self) -> dict:
        return {
            **super().declaration(),
            "type": self.check_type,
            "capacity": self.check_capacity,
            "programme": self.check_programme,
        }


# ── the rulebook ──────────────────────────────────────────────────────────
#
# H-numbering is preserved from the institution's authoritative reference so the
# two documents stay mutually readable. H5 does not exist (no prayer rule, D2)
# and H13 is structurally not applicable (single-gender snapshots, D1).

RULEBOOK: tuple[RuleSpec, ...] = (
    ExactMeetingMultiset("H1", "Meeting count & duration"),
    DistinctDays("H2", "All-different-days", group="section"),
    LegalWindow("H3", "Legal slot grid"),
    NoOverlapByKey("H7", "Instructor clash", key="instructor"),
    DailyCardinality("H8", "Instructor daily cap", key="instructor", cap=3),
    NoOverlapByKey("H9", "Room exclusivity", key="room"),
    NoOverlapByKey("H10", "Same-offering separation", key="offering"),
    RoomCompatibility("H11_H12_H14", "Room type / capacity / programme"),
)

#: Rules that exist in the institution's reference but cannot be graded here yet,
#: each with the reason. Declared so the checker reports them rather than
#: silently omitting them — an unlisted rule is indistinguishable from a passing
#: one, which is how a checker starts lying.
DECLARED_GAPS: tuple[tuple[str, str, Enforcement, str], ...] = (
    (
        "H4",
        "Blocked slots",
        Enforcement.COVERAGE_GAP,
        "no blocked-slot input is carried on the snapshot yet",
    ),
    (
        "H6",
        "Locked placements",
        Enforcement.COVERAGE_GAP,
        "locks are not modelled until boards are persisted (S5)",
    ),
    (
        "H13",
        "Room gender",
        Enforcement.NOT_APPLICABLE,
        "a snapshot is single-gender (D1); the room pool is filtered at intake",
    ),
    (
        "H15",
        "Critical student overlap",
        Enforcement.EVIDENCE_GAP,
        "requires a student sectioning snapshot (S4)",
    ),
    (
        "H16",
        "Section capacity / reserve",
        Enforcement.EVIDENCE_GAP,
        "requires a student sectioning snapshot (S4)",
    ),
)


def rulebook_fingerprint() -> str:
    """Hash of the declarations — tells you whether two runs were judged alike.

    Full SHA-256, not truncated: a fingerprint that decides comparability is not
    a cache key.
    """
    payload = {
        "rules": [r.declaration() for r in RULEBOOK],
        "gaps": [[rid, title, mode.value, why] for rid, title, mode, why in DECLARED_GAPS],
    }
    blob = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


# ── projection 1: whole-board checking ────────────────────────────────────
#
# The only execution path S2 needs. Delta checking and CP-SAT compilation are
# later projections of the *same* declarations, which is the point.

Checker = Callable[[RuleSpec, Snapshot, Board], tuple[Violation, ...]]


def _pairs(items: Iterable[Placement]) -> Iterable[tuple[Placement, Placement]]:
    ordered = sorted(items, key=lambda p: (p.day.index, p.window.start, p.id))
    for i, a in enumerate(ordered):
        for b in ordered[i + 1 :]:
            if b.day is not a.day:
                continue
            if b.window.start >= a.window.end:
                break  # sorted by start: nothing later can overlap
            yield a, b


def _check_no_overlap(spec: NoOverlapByKey, snap: Snapshot, board: Board) -> tuple[Violation, ...]:
    # D19: sections of a fixed-block course are REQUIRED to share their hours,
    # so grading them for overlapping each other reports the rule being obeyed
    # as a failure. Scoped to the offering key alone — an instructor cannot be
    # in two rooms at once no matter what the course is, and neither can a room.
    exempt: frozenset[str] = frozenset()
    if spec.key == "offering" and spec.exempt_fixed_blocks:
        exempt = frozenset(
            offering.id for offering in snap.offerings if offering.occupies_fixed_block
        )
    grouped: dict[str, list[Placement]] = {}
    for p in board.placements:
        for key in spec.keys_of(p):
            if key in exempt:
                continue
            grouped.setdefault(key, []).append(p)
    out: list[Violation] = []
    for key, items in sorted(grouped.items()):
        for a, b in _pairs(items):
            if a.section_id == b.section_id and spec.key == "offering":
                continue  # a section vs itself is H2's business, not H10's
            out.append(
                Violation(
                    spec.rule_id,
                    f"{spec.key} {key}: {a.id} {a.day.value} {a.window} overlaps {b.id} {b.window}",
                    (a.id, b.id),
                )
            )
    return tuple(out)


def _fixed_block_sections(snap: Snapshot) -> frozenset[str]:
    """Sections whose hours are given, not chosen (D19)."""
    offerings = snap.offerings_by_id
    return frozenset(
        section.id
        for section in snap.sections
        if offerings[section.offering_id].occupies_fixed_block
    )


def _check_distinct_days(spec: DistinctDays, snap: Snapshot, board: Board) -> tuple[Violation, ...]:
    out: list[Violation] = []
    refined = _fixed_block_sections(snap) if spec.refine_for_fixed_blocks else frozenset()
    for section_id, items in sorted(board.by_section.items()):
        fixed = section_id in refined
        seen: dict[object, Placement] = {}
        for p in sorted(items, key=lambda x: (x.day.index, x.window.start)):
            # For a fixed-block section the key is the CELL, not the day: it is
            # meant to meet twice a morning, and what would be wrong is meeting
            # twice in the SAME hour.
            key = (p.day.value, p.window.start) if fixed else p.day.value
            if key in seen:
                out.append(
                    Violation(
                        spec.rule_id,
                        f"section {section_id} meets twice at {p.day.value} {p.window}"
                        if fixed
                        else f"section {section_id} meets twice on {p.day.value}",
                        (seen[key].id, p.id),
                    )
                )
            else:
                seen[key] = p
    return tuple(out)


def _check_daily_cap(spec: DailyCardinality, snap: Snapshot, board: Board) -> tuple[Violation, ...]:
    counts: dict[tuple[str, str], list[Placement]] = {}
    for p in board.placements:
        if p.instructor_id is None:
            continue  # unassigned sections carry no instructor constraint (D5)
        counts.setdefault((str(p.instructor_id), p.day.value), []).append(p)
    return tuple(
        Violation(
            spec.rule_id,
            f"instructor {key} has {len(items)} sessions on {day} (cap {spec.cap})",
            tuple(p.id for p in items),
        )
        for (key, day), items in sorted(counts.items())
        if len(items) > spec.cap
    )


def _check_legal_window(spec: LegalWindow, snap: Snapshot, board: Board) -> tuple[Violation, ...]:
    legal = {
        (duration, delivery): set(snap.grid.windows_for(duration, delivery))
        for duration, delivery in {(p.window.duration, p.delivery) for p in board.placements}
    }
    days = set(snap.grid.days())
    out: list[Violation] = []
    for p in sorted(board.placements, key=lambda x: x.id):
        if p.day not in days:
            out.append(
                Violation(spec.rule_id, f"{p.id}: {p.day.value} is not a teaching day", (p.id,))
            )
        elif p.window not in legal.get((p.window.duration, p.delivery), set()):
            out.append(
                Violation(
                    spec.rule_id,
                    f"{p.id}: {p.window} ({p.window.duration}min) is not a declared window",
                    (p.id,),
                )
            )
    return tuple(out)


def _check_multiset(
    spec: ExactMeetingMultiset, snap: Snapshot, board: Board
) -> tuple[Violation, ...]:
    offerings = snap.offerings_by_id
    placed_by_section = board.by_section
    out: list[Violation] = []
    for section in sorted(snap.sections, key=lambda s: s.id):
        offering = offerings[section.offering_id]
        required: dict[tuple, int] = {}
        for r in offering.requirements:
            required[(r.kind, r.delivery, r.duration)] = (
                required.get((r.kind, r.delivery, r.duration), 0) + r.count_per_week
            )
        placed: dict[tuple, int] = {}
        for p in placed_by_section.get(section.id, ()):
            placed[(p.kind, p.delivery, p.window.duration)] = (
                placed.get((p.kind, p.delivery, p.window.duration), 0) + 1
            )
        if required != placed:
            out.append(
                Violation(
                    spec.rule_id,
                    f"{section.id} ({offering.course_code}): placed "
                    f"{_shape(placed)} but requires {_shape(required)}",
                    (section.id,),
                )
            )
    return tuple(out)


def _shape(counts: dict[tuple, int]) -> str:
    if not counts:
        return "nothing"
    return ", ".join(
        f"{n}x{kind.value}/{delivery.value}/{dur}min"
        for (kind, delivery, dur), n in sorted(counts.items(), key=lambda kv: str(kv[0]))
    )


def _check_room_compat(
    spec: RoomCompatibility, snap: Snapshot, board: Board
) -> tuple[Violation, ...]:
    rooms = {r.id: r for r in snap.rooms}
    offerings = snap.offerings_by_id
    sections = {s.id: s for s in snap.sections}
    out: list[Violation] = []
    for p in sorted(board.placements, key=lambda x: x.id):
        if p.room_id is None:
            continue  # unroomed is legitimate (D7); H_ROOM_REQUIRED observes it
        room = rooms.get(p.room_id)
        if room is None:
            out.append(
                Violation(spec.rule_id, f"{p.id}: room {p.room_id} is not in the pool", (p.id,))
            )
            continue
        if spec.check_type and room.kind is not p.kind:
            out.append(
                Violation(
                    spec.rule_id,
                    f"{p.id}: needs a {p.kind.value} room, {room.code} is {room.kind.value}",
                    (p.id,),
                )
            )
        section = sections.get(p.section_id)
        if spec.check_capacity and section is not None:
            need = snap.policy.required_room_capacity(section.capacity)
            if room.capacity < need:
                out.append(
                    Violation(
                        spec.rule_id,
                        f"{p.id}: {room.code} holds {room.capacity}, needs {need}",
                        (p.id,),
                    )
                )
        offering = offerings.get(p.offering_id)
        if (
            spec.check_programme
            and offering is not None
            and not (offering.programs & room.programs)
        ):
            out.append(
                Violation(
                    spec.rule_id,
                    f"{p.id}: {room.code} serves {sorted(room.programs)}, "
                    f"course needs {sorted(offering.programs)}",
                    (p.id,),
                )
            )
    return tuple(out)


_CHECKERS: dict[type, Checker] = {
    NoOverlapByKey: _check_no_overlap,
    DistinctDays: _check_distinct_days,
    DailyCardinality: _check_daily_cap,
    LegalWindow: _check_legal_window,
    ExactMeetingMultiset: _check_multiset,
    RoomCompatibility: _check_room_compat,
}


def check_rule(spec: RuleSpec, snapshot: Snapshot, board: Board) -> RuleResult:
    checker = _CHECKERS.get(type(spec))
    if checker is None:
        return RuleResult(
            spec.rule_id,
            spec.title,
            Enforcement.COVERAGE_GAP,
            spec.severity,
            note="no checker projection for this rule type",
        )
    return RuleResult(
        spec.rule_id,
        spec.title,
        Enforcement.CHECK,
        spec.severity,
        violations=checker(spec, snapshot, board),
    )

"""Readiness — what this input can and cannot produce, *before* any solving.

Owner decision D4: **readiness is informational, not a gate.** Missing data gets
completed by real use of the app; it is not a design problem to engineer around.
So this reports what a registrar needs to know and then gets out of the way.

**Where the blocking line sits (owner decision D6).** Time is structural: a
meeting that has no legal window cannot exist, so that blocks. A **room is an
assignment that can be left unmade** — if there are not enough rooms, the builder
still produces the timetable and simply reports those meetings as *unassigned
room*. A room shortage therefore never blocks.

That is deliberately not the old engine's behaviour. It also left meetings
unroomed — but silently, as a bare `UNASSIGNED` string with no count, no cause
and no way to tell an unavoidable inventory shortage from a solver that gave up.
Here the shortage is stated up front, with the arithmetic that proves it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from scheduler.domain import MeetingKind, Snapshot

# Elective placeholders are recognised by shape, not by a list. The list that
# used to live here had already drifted out of date — it omitted AI2, AI3, DS3,
# GSE2 and GSE3, every one of which appears in the live plan — and it silently
# disagreed with the rule used elsewhere. One question, one answer.
from scheduler.intake import is_placeholder_code  # noqa: E402


class Severity(str, Enum):
    BLOCKING = "blocking"  # solving cannot produce a valid board
    WARNING = "warning"  # solvable, but a known shortage will bite
    INFO = "info"  # scope/assumption a human should be aware of


@dataclass(frozen=True)
class Finding:
    severity: Severity
    code: str
    message: str
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "severity": self.severity.value,
            "code": self.code,
            "message": self.message,
            "detail": self.detail,
        }


@dataclass
class ReadinessReport:
    snapshot_summary: dict
    findings: list[Finding] = field(default_factory=list)

    @property
    def blocking(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.BLOCKING]

    @property
    def status(self) -> str:
        return "BLOCKED_INPUT" if self.blocking else "READY"

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "snapshot": self.snapshot_summary,
            "findings": [f.as_dict() for f in self.findings],
        }


def assess(snapshot: Snapshot) -> ReadinessReport:
    report = ReadinessReport(snapshot_summary=snapshot.summary())
    add = report.findings.append
    grid = snapshot.grid

    # ── 0a. Students excluded at intake ────────────────────────────────────
    if snapshot.excluded_withdrawn:
        add(
            Finding(
                Severity.INFO,
                "WITHDRAWN_STUDENTS_EXCLUDED",
                f"{snapshot.excluded_withdrawn} student(s) were left out of demand "
                f"because their status says WITHDRAWN. They will not attend, and "
                f"counting them would inflate the section count.",
                {"excluded": snapshot.excluded_withdrawn},
            )
        )

    # ── 0. A course with more sections than the week has room for ──────────
    #
    # H10 forbids two sections of one course from ever overlapping, so N sections
    # need N mutually non-overlapping cells. The week supplies a fixed number of
    # those, and once a course exceeds it the model is INFEASIBLE — no timetable
    # exists, at any budget.
    #
    # This is checked here, before solving, because the failure is otherwise
    # mute: the female CS cohort came back INFEASIBLE with the single note "no
    # assignment found within 60s", which reads like a solver that needed longer.
    # It did not. CS491 is recorded as "GRADUATION I" while its AI counterpart is
    # "GRADUATION PROJECT I", so the name-matching rule that excludes graduation
    # projects missed it, and a project nobody timetables was planned with 31
    # sections against 25 available cells.
    #
    # The arithmetic is checked rather than the spelling, so the next variant
    # spelling — and every other cause of the same shape — is caught too.
    for offering in snapshot.offerings:
        siblings = len(snapshot.sections_by_offering.get(offering.id, ()))
        if siblings < 2:
            continue
        durations = frozenset(
            r.duration for r in offering.requirements if r.needs_room
        ) or frozenset(r.duration for r in offering.requirements)
        if not durations:
            continue
        capacity = grid.max_nonoverlapping_per_day(durations) * len(grid.days())
        # It is MEETINGS that must not overlap, not sections. H10 separates every
        # meeting of every sibling from every other, so a course with 17 sections
        # meeting three times a week needs 51 mutually free cells, not 17. Getting
        # this wrong is what let the female CS cohort stay infeasible after the
        # first fix: the section count looked comfortable at 17 against 25.
        meetings_per_section = sum(r.count_per_week for r in offering.requirements)
        needed = siblings * meetings_per_section
        if needed > capacity:
            add(
                Finding(
                    Severity.BLOCKING,
                    "MORE_SECTIONS_THAN_THE_WEEK_HOLDS",
                    f"{offering.course_code} plans {siblings} sections meeting "
                    f"{meetings_per_section} times a week = {needed} meetings, and "
                    f"no two of them may overlap, but the week holds only "
                    f"{capacity} non-overlapping slots of that length. No timetable "
                    f"exists until the section count drops, the course meets less "
                    f"often, or the grid grows.",
                    {
                        "course": offering.course_code,
                        "course_name": offering.course_name,
                        "sections": siblings,
                        "meetings_per_section": meetings_per_section,
                        "meetings_needed": needed,
                        "weekly_capacity": capacity,
                        "excess": needed - capacity,
                    },
                )
            )

    # ── 1. Meeting lengths the grid declares no window for ─────────────────
    # Timing family is decided by DURATION, room family by KIND — so a 100-minute
    # lecture legitimately runs at the 100-minute (lab-timing) windows while
    # occupying a lecture room. Only a length with no declared window anywhere is
    # genuinely unplaceable.
    unplaceable: dict[int, list[str]] = {}
    for offering in snapshot.offerings:
        for requirement in offering.requirements:
            if not grid.windows_for(requirement.duration):
                unplaceable.setdefault(requirement.duration, []).append(offering.course_code)
    for duration, codes in sorted(unplaceable.items()):
        add(
            Finding(
                Severity.BLOCKING,
                "NO_LEGAL_WINDOW_FOR_DURATION",
                f"{len(codes)} offerings need a {duration}-minute meeting, but the "
                f"grid declares no window of that length at all.",
                {"duration": duration, "offerings": sorted(codes)[:20], "count": len(codes)},
            )
        )

    # ── 2. Room supply vs demand, per kind (a counting proof, not a guess) ──
    required = snapshot.physical_meetings_required()
    for kind in MeetingKind:
        rooms = snapshot.rooms_of_kind(kind)
        need = required.get(kind, 0)
        if need == 0:
            continue
        if not rooms:
            add(
                Finding(
                    Severity.WARNING,
                    "NO_ROOMS_OF_KIND",
                    f"{need} {kind.value} meetings/week require a {kind.value} room, "
                    f"but this cohort has none — all {need} will be scheduled in time "
                    f"with **no room assigned**.",
                    {"kind": kind.value, "meetings_required": need, "will_be_unroomed": need},
                )
            )
            continue
        # Supply is bounded by what a room can actually hold in a day, not by the
        # number of declared cells — overlapping alternatives are one opportunity
        # offered twice, never two.
        durations = frozenset(
            r.duration
            for o in snapshot.offerings
            for r in o.requirements
            if r.needs_room and r.kind is kind
        )
        per_day = grid.max_nonoverlapping_per_day(durations) if durations else 0
        supply = len(rooms) * len(grid.days()) * per_day
        if per_day and need > supply:
            add(
                Finding(
                    Severity.WARNING,
                    "ROOM_PERIOD_SHORTAGE",
                    f"{kind.value}: {need} meetings/week need placing, but "
                    f"{len(rooms)} rooms x {len(grid.days())} days x {per_day} "
                    f"non-overlapping/day = {supply} room-periods exist. "
                    f"At least {need - supply} meetings will be scheduled in time "
                    f"with **no room assigned** — this does not block the build.",
                    {
                        "kind": kind.value,
                        "meetings_required": need,
                        "rooms": len(rooms),
                        "days": len(grid.days()),
                        "max_nonoverlapping_per_room_day": per_day,
                        "room_periods": supply,
                        "minimum_unroomed": need - supply,
                    },
                )
            )
        elif supply and need > supply * 0.85:
            add(
                Finding(
                    Severity.WARNING,
                    "ROOM_PRESSURE",
                    f"{kind.value}: {need}/{supply} room-periods used "
                    f"({100 * need / supply:.0f}%) — little slack for a feasible packing.",
                    {"kind": kind.value, "utilisation": round(100 * need / supply, 1)},
                )
            )

    # ── 3. Seats vs demand, per offering ───────────────────────────────────
    index = snapshot.demand_index
    sections_by_offering = snapshot.sections_by_offering
    short: list[dict] = []
    for offering in snapshot.offerings:
        if not offering.is_scheduled:
            continue  # graduation projects have no sections by design, not by shortage
        want = index.by_offering.get(offering.id, 0)
        if want == 0:
            continue
        seats = sum(s.capacity for s in sections_by_offering.get(offering.id, ()))
        if seats < want:
            short.append(
                {
                    "course": offering.course_code,
                    "demand": want,
                    "seats": seats,
                    "short": want - seats,
                }
            )
    if short:
        add(
            Finding(
                Severity.WARNING,
                "SEAT_SHORTAGE",
                f"{len(short)} offerings have fewer planned seats than demand.",
                {"offerings": sorted(short, key=lambda r: -r["short"])[:20]},
            )
        )

    # ── 4. Assumed capacities (D3 — the fallback must never be silent) ─────
    assumed = [o.course_code for o in snapshot.offerings if not o.capacity_is_declared]
    if assumed:
        add(
            Finding(
                Severity.INFO,
                "ASSUMED_CAPACITY",
                f"{len(assumed)} offerings have no declared max_capacity; the policy "
                f"default of {snapshot.policy.default_capacity} was assumed.",
                {
                    "default": snapshot.policy.default_capacity,
                    "offerings": sorted(assumed)[:30],
                    "count": len(assumed),
                },
            )
        )

    # ── 5. Instructor eligibility vs assignment — SCOPE, never an error (D5) ─
    # These are different things and conflating them is how the old engine
    # manufactured both phantom clashes and flattering coverage numbers.
    # Eligibility says who *may* teach an offering; assignment says who *does*
    # teach a section. No assignment step exists yet (that is S3).
    covered, total = snapshot.instructor_coverage
    offerings_with_eligible = len(
        {oid for i in snapshot.instructors for oid in i.eligible_offerings}
    )
    add(
        Finding(
            Severity.INFO,
            "INSTRUCTOR_SCOPE",
            f"{len(snapshot.instructors)} instructors are eligible across "
            f"{offerings_with_eligible}/{len(snapshot.offerings)} offerings; "
            f"{covered}/{total} sections have an assignment. Partial linkage is "
            f"expected permanently — instructor constraints and metrics apply to "
            f"the assigned subset only, and are never reported without this scope.",
            {
                "eligible_instructors": len(snapshot.instructors),
                "offerings_with_eligible_instructor": offerings_with_eligible,
                "offerings_total": len(snapshot.offerings),
                "sections_assigned": covered,
                "sections_total": total,
            },
        )
    )

    # ── 6. Elective placeholders masquerading as offerings ─────────────────
    # A placeholder (AI1, DS2, CS1 …) is a slot in the plan, not a teachable
    # course; it resolves to a real elective per term. Scheduling the
    # placeholder itself would build a timetable for a course that does not
    # exist — the same defect that made the old assignment screen unusable.
    placeholders = [o.course_code for o in snapshot.offerings if is_placeholder_code(o.course_code)]
    if placeholders:
        add(
            Finding(
                Severity.WARNING,
                "UNRESOLVED_ELECTIVE_PLACEHOLDER",
                f"{len(placeholders)} plan entries are elective placeholders, not real "
                f"courses. They must resolve to concrete electives before scheduling.",
                {"placeholders": sorted(placeholders)},
            )
        )

    # ── 7. Course categories that are deliberately not roomed / not timetabled ─
    unscheduled = [o.course_code for o in snapshot.offerings if not o.is_scheduled]
    if unscheduled:
        add(
            Finding(
                Severity.INFO,
                "NOT_TIMETABLED",
                f"{len(unscheduled)} offerings are graduation projects or cooperative "
                f"training: they carry credit but have no weekly meeting, so they get "
                f"no slot and no room.",
                {"offerings": sorted(unscheduled)},
            )
        )
    online = [o.course_code for o in snapshot.offerings if o.is_scheduled and o.is_fully_online]
    if online:
        add(
            Finding(
                Severity.INFO,
                "ONLINE_NO_ROOM",
                f"{len(online)} offerings are delivered online (GS/GSE): they occupy "
                f"student and instructor time but need no room and never contribute to "
                f"an on-campus gap.",
                {"offerings": sorted(online)},
            )
        )

    # ── 8. Offerings nobody needs this term ────────────────────────────────
    idle = [o.course_code for o in snapshot.offerings if index.by_offering.get(o.id, 0) == 0]
    if idle:
        add(
            Finding(
                Severity.INFO,
                "NO_DEMAND",
                f"{len(idle)} planned offerings have no demand this term and were "
                f"given no sections.",
                {"offerings": sorted(idle)[:30], "count": len(idle)},
            )
        )

    return report

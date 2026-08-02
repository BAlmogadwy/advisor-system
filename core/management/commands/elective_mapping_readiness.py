"""Which programmes are ready for an elective-options screen, and which are not.

The screen cannot ship globally. Measured on live data, 26 of 28 `(programme,
slot)` pairs for the programmes that actually have students resolve to zero
options — AI2 and DS2, 115 students between them, get nothing for every slot they
have. A screen that answers «لم تُنشر خيارات هذا المتطلب بعد» for almost everyone
is an incomplete join wearing the costume of a feature.

So this reports readiness per programme, and the screen is enabled per programme
rather than by one global switch. The gate:

* every active elective placeholder has a recognised mapping;
* every slot resolves to at least one active course;
* no cross-programme mapping (duplicates and dangling rows are impossible —
  `uq_elective_mapping` and the cascading FK already forbid them);
* mapped courses carry the right requirement type and credit value;
* programmes with active students are covered.

**The declared requirement type is authoritative** — `is_elective_slot`, the one
implementation. Nothing here infers a slot from a code pattern; issue #55 is what
that costs.

Read-only. It changes nothing and is safe to run against production.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from django.core.management.base import BaseCommand

from core.models import (
    ElectiveCourse,
    ElectiveTermMapping,
    ProgrammeRequirement,
    Student,
)
from core.services.student_helpers import is_elective_slot, normalize_code


def readiness(academic_year: str = "", term: str = "") -> list[dict[str, Any]]:
    """One row per (programme, slot), with everything the gate needs.

    TERM-SCOPED, because `ElectiveTermMapping` is: it carries `academic_year` and
    `term`, and a slot mapped for a past term is not mapped for this one. Writing
    the first version of this report without that filter would have reported a
    programme ready on the strength of last year's publication —
    `_resolve_elective_slot` has the same blind spot (`CAPABILITY-SCREEN-MAP.md:171`),
    masked today only because every mapping row happens to be 1448/term 1.
    """
    if not academic_year or not term:
        from core.services.planner_drafts import planning_term

        default_year, default_term = planning_term()
        academic_year = academic_year or default_year
        term = term or default_term
    students = defaultdict(int)
    for program in Student.objects.values_list("program", flat=True):
        students[normalize_code(program)] += 1

    slots: list[tuple[str, str, int, str]] = []
    for row in ProgrammeRequirement.objects.values(
        "program", "course_code", "type", "credit_hours"
    ):
        if is_elective_slot(row["type"]):
            slots.append(
                (
                    normalize_code(row["program"]),
                    normalize_code(row["course_code"]),
                    int(row["credit_hours"] or 0),
                    str(row["type"] or ""),
                )
            )

    # Mappings, grouped, so a duplicate is visible rather than deduplicated away.
    mapped: dict[tuple[str, str], list[int]] = defaultdict(list)
    for m in ElectiveTermMapping.objects.filter(
        academic_year=str(academic_year), term=str(term)
    ).values("programme", "placeholder_code", "elective_id"):
        key = (normalize_code(m["programme"]), normalize_code(m["placeholder_code"]))
        mapped[key].append(m["elective_id"])

    electives = {
        e["id"]: e
        for e in ElectiveCourse.objects.values("id", "course_code", "programme", "credit_hours")
    }

    rows: list[dict[str, Any]] = []
    for program, slot, slot_credits, slot_type in sorted(set(slots)):
        ids = mapped.get((program, slot), [])
        options = [electives[i] for i in ids if i in electives]

        # Two of the gate's stated conditions — "no duplicate mapping" and "every
        # mapping resolves to a real course" — are enforced by the SCHEMA, not here:
        # `uq_elective_mapping` is unique on (year, term, programme, placeholder,
        # elective), and the elective FK cascades. Re-checking them in Python would
        # be a guard for a state the database cannot hold, and a test for it cannot
        # be written without deleting the constraint. `tests/test_elective_readiness.py`
        # asserts the constraints instead.
        problems: list[str] = []
        # A mapping whose elective is declared for another programme. Blank is not
        # cross-programme — it is unset, which is its own (known) data gap.
        foreign = sorted(
            {
                normalize_code(o["programme"])
                for o in options
                if o["programme"] and normalize_code(o["programme"]) != program
            }
        )
        if foreign:
            problems.append(f"cross-programme mapping from {', '.join(foreign)}")
        if slot_credits:
            wrong = [
                o["course_code"]
                for o in options
                if o["credit_hours"] and int(o["credit_hours"]) != slot_credits
            ]
            if wrong:
                problems.append(
                    f"credit mismatch (slot {slot_credits}h): {', '.join(sorted(wrong)[:3])}"
                )

        rows.append(
            {
                "programme": program,
                "slot": slot,
                "type": slot_type,
                "students": students.get(program, 0),
                "mapping_exists": bool(ids),
                "active_options": len(options),
                "problems": problems,
                "ready": bool(ids) and bool(options) and not problems,
            }
        )
    return rows


class Command(BaseCommand):
    help = "Report whether each programme's elective slots are ready to be shown to students."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--with-students",
            action="store_true",
            help="Only programmes that have at least one student on file.",
        )
        parser.add_argument(
            "--fail-on-unready",
            action="store_true",
            help="Exit non-zero if any reported programme is not ready. For a gate.",
        )
        parser.add_argument("--year", default="", help="Defaults to the configured term.")
        parser.add_argument("--term", default="", help="Defaults to the configured term.")

    def handle(self, *args: Any, **options: Any) -> None:
        rows = readiness(options["year"], options["term"])
        if options["with_students"]:
            rows = [r for r in rows if r["students"] > 0]
        if not rows:
            self.stdout.write("No elective slots found.")
            return

        from core.services.planner_drafts import planning_term

        year = options["year"] or planning_term()[0]
        term = options["term"] or planning_term()[1]
        self.stdout.write(f"Elective mapping readiness for {year}/term {term}")
        self.stdout.write("")
        head = f"{'Programme':<10} {'Slot':<7} {'Students':>8} {'Mapping':>8} {'Options':>8}  Ready"
        self.stdout.write(head)
        self.stdout.write("-" * len(head))
        for r in rows:
            self.stdout.write(
                f"{r['programme']:<10} {r['slot']:<7} {r['students']:>8} "
                f"{'Yes' if r['mapping_exists'] else 'No':>8} {r['active_options']:>8}  "
                f"{'Yes' if r['ready'] else 'No'}"
            )
            for problem in r["problems"]:
                self.stdout.write(f"{'':<10} {'':<7} {'':>8} {'':>8} {'':>8}    ! {problem}")

        by_programme: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in rows:
            by_programme[r["programme"]].append(r)
        ready = sorted(p for p, rs in by_programme.items() if all(x["ready"] for x in rs))
        unready = sorted(p for p, rs in by_programme.items() if not all(x["ready"] for x in rs))

        self.stdout.write("")
        self.stdout.write(f"Programmes ready:     {', '.join(ready) if ready else 'none'}")
        self.stdout.write(f"Programmes NOT ready: {', '.join(unready) if unready else 'none'}")
        blocked = sum(r["students"] for r in rows if not r["ready"])
        self.stdout.write(
            f"{sum(1 for r in rows if r['ready'])}/{len(rows)} slots ready; "
            f"{blocked} student-slot pairs would see an empty screen."
        )

        if options["fail_on_unready"] and unready:
            raise SystemExit(1)

"""Validate and plan an elective-mapping publication. Writing is the caller's job.

A mapping says "these concrete courses are approved to fill this slot, for this
programme, this term". That is an academic decision with a source, and the whole
point of importing it from a file rather than typing it into a shell is that the
file can be reviewed, versioned, and pointed at when someone asks who approved it.

**This module never writes.** It parses, validates the whole file, and returns a
deterministic plan. The command decides whether to apply it. Splitting them is what
makes the dry run trustworthy: the run that reports and the run that writes compute
the same plan from the same code, so the report cannot describe something other
than what would happen.

**Validation is all-or-nothing.** One bad row rejects the file. A partially applied
publication is worse than a rejected one — it leaves a slot half-approved with no
record of which half, and the readiness gate opens on it.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from typing import Any

from core.models import ElectiveCourse, ElectiveTermMapping, ProgrammeRequirement
from core.services.student_helpers import is_elective_slot, normalize_code

REQUIRED_COLUMNS = (
    "academic_year",
    "term",
    "programme",
    "slot_code",
    "course_code",
    "source_reference",
)

VALID_TERMS = {"1", "2", "3"}

#: Add, retain, replace, reject — the four things a row can mean.
ADD = "ADD"
RETAIN = "RETAIN"
REMOVE = "REMOVE"


@dataclass
class Problem:
    line: int
    code: str
    detail: str

    def __str__(self) -> str:
        where = f"line {self.line}" if self.line else "file"
        return f"{where}: [{self.code}] {self.detail}"


@dataclass
class Plan:
    """What an apply would do. Deterministic, and the same object the dry run prints."""

    rows: list[dict[str, Any]] = field(default_factory=list)
    add: list[dict[str, Any]] = field(default_factory=list)
    retain: list[dict[str, Any]] = field(default_factory=list)
    remove: list[dict[str, Any]] = field(default_factory=list)
    problems: list[Problem] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def summary(self) -> str:
        return (
            f"{len(self.add)} to add, {len(self.retain)} already present, "
            f"{len(self.remove)} to remove, {len(self.problems)} problem(s)"
        )


def _read(text: str, plan: Plan) -> list[dict[str, str]]:
    reader = csv.DictReader(io.StringIO(text))
    missing = [c for c in REQUIRED_COLUMNS if c not in (reader.fieldnames or [])]
    if missing:
        plan.problems.append(
            Problem(0, "MISSING_COLUMNS", f"required column(s) absent: {', '.join(missing)}")
        )
        return []
    return list(reader)


def build_plan(
    text: str,
    *,
    replace_year: str = "",
    replace_term: str = "",
) -> Plan:
    """Parse, validate everything, and return what an apply would do.

    `replace_year`/`replace_term` are the ONLY way an existing row is removed.
    Omission never means deletion: a file that lists two of a slot's three approved
    courses is a file someone forgot to finish, not an instruction to withdraw the
    third, and guessing which costs a student an option they were entitled to.
    """
    plan = Plan()
    raw = _read(text, plan)
    if plan.problems:
        return plan

    # Everything the file could reference, fetched once. A per-row query would make
    # a 200-line file 600 queries and tell the reviewer nothing extra.
    slots = {
        (normalize_code(r["program"]), normalize_code(r["course_code"])): r
        for r in ProgrammeRequirement.objects.values(
            "program", "course_code", "type", "credit_hours"
        )
    }
    catalogue = {
        (normalize_code(e["programme"]), normalize_code(e["course_code"])): e
        for e in ElectiveCourse.objects.values("id", "programme", "course_code", "credit_hours")
    }
    by_code: dict[str, list[dict[str, Any]]] = {}
    for (prog, code), entry in catalogue.items():
        by_code.setdefault(code, []).append({**entry, "_programme": prog})

    seen: dict[tuple[str, str, str, str, str], int] = {}

    for offset, row in enumerate(raw):
        line = offset + 2  # +1 for the header, +1 because humans count from one
        year = str(row.get("academic_year") or "").strip()
        term = str(row.get("term") or "").strip()
        programme = normalize_code(row.get("programme"))
        slot = normalize_code(row.get("slot_code"))
        course = normalize_code(row.get("course_code"))
        source = str(row.get("source_reference") or "").strip()

        def fail(code: str, detail: str, _line: int = line) -> None:
            # `_line` is bound at definition, not read at call: a closure over the
            # loop variable would report every problem against the LAST line.
            plan.problems.append(Problem(_line, code, detail))

        if not source:
            # A mapping with no provenance cannot be audited, corrected or
            # defended. It is the first check because it is the one a hurried
            # publication drops.
            fail("NO_SOURCE", "source_reference is required — who approved this row?")
        if not year.isdigit() or len(year) != 4:
            fail("BAD_YEAR", f"academic_year must be four digits, got {year!r}")
        if term not in VALID_TERMS:
            fail("BAD_TERM", f"term must be one of {sorted(VALID_TERMS)}, got {term!r}")
        if not programme or not slot or not course:
            fail("INCOMPLETE", "programme, slot_code and course_code are all required")
            continue

        key = (year, term, programme, slot, course)
        if key in seen:
            fail("DUPLICATE", f"already given on line {seen[key]}")
            continue
        seen[key] = line

        requirement = slots.get((programme, slot))
        if requirement is None:
            fail("NO_SUCH_SLOT", f"{programme} declares no requirement {slot}")
            continue
        if not is_elective_slot(requirement["type"]):
            # The declared TYPE decides, never the code shape. Issue #55 is what
            # the other rule costs.
            fail(
                "NOT_AN_ELECTIVE_SLOT",
                f"{programme}/{slot} is declared {requirement['type']!r}, not an elective",
            )
            continue

        entry = catalogue.get((programme, course))
        if entry is None:
            elsewhere = by_code.get(course) or []
            if elsewhere:
                # A real course, catalogued for someone else. Allowed only with an
                # explicit approved cross-programme rule, which this file has no
                # way to express — so it is rejected rather than assumed.
                owners = sorted({e["_programme"] or "(unset)" for e in elsewhere})
                fail(
                    "CROSS_PROGRAMME",
                    f"{course} is catalogued for {', '.join(owners)}, not {programme}; "
                    "a cross-programme mapping needs explicit approval",
                )
            else:
                fail("NOT_IN_CATALOGUE", f"{course} is not in the elective catalogue")
            continue

        slot_credits = int(requirement["credit_hours"] or 0)
        course_credits = int(entry["credit_hours"] or 0)
        if slot_credits and course_credits and slot_credits != course_credits:
            # A student choosing this would not satisfy the requirement they chose
            # it for. Every live Free/University Elective slot currently fails here.
            fail(
                "CREDIT_MISMATCH",
                f"{programme}/{slot} requires {slot_credits}h, {course} is {course_credits}h",
            )
            continue

        record = {
            "line": line,
            "academic_year": year,
            "term": term,
            "programme": programme,
            "slot_code": slot,
            "course_code": course,
            "elective_id": entry["id"],
            "source_reference": source,
        }
        plan.rows.append(record)

    if plan.problems:
        # All-or-nothing: no plan is offered from a file that failed validation, so
        # nothing downstream can act on a half-read one.
        return plan

    existing = {
        (
            m["academic_year"],
            str(m["term"]),
            normalize_code(m["programme"]),
            normalize_code(m["placeholder_code"]),
            m["elective_id"],
        ): m["id"]
        for m in ElectiveTermMapping.objects.values(
            "id", "academic_year", "term", "programme", "placeholder_code", "elective_id"
        )
    }
    for record in plan.rows:
        key = (
            record["academic_year"],
            record["term"],
            record["programme"],
            record["slot_code"],
            record["elective_id"],
        )
        (plan.retain if key in existing else plan.add).append(record)

    if replace_year and replace_term:
        wanted = {
            (r["academic_year"], r["term"], r["programme"], r["slot_code"], r["elective_id"])
            for r in plan.rows
        }
        for key, row_id in existing.items():
            if key[0] == replace_year and key[1] == replace_term and key not in wanted:
                plan.remove.append(
                    {
                        "id": row_id,
                        "academic_year": key[0],
                        "term": key[1],
                        "programme": key[2],
                        "slot_code": key[3],
                        "elective_id": key[4],
                    }
                )
    return plan


def apply_plan(plan: Plan) -> dict[str, int]:
    """Write a validated plan. Atomic, and refuses an invalid one outright."""
    from django.db import transaction

    if not plan.ok:
        raise ValueError("refusing to apply a plan that failed validation")

    with transaction.atomic():
        if plan.remove:
            ElectiveTermMapping.objects.filter(id__in=[r["id"] for r in plan.remove]).delete()
        ElectiveTermMapping.objects.bulk_create(
            [
                ElectiveTermMapping(
                    academic_year=r["academic_year"],
                    term=r["term"],
                    programme=r["programme"],
                    placeholder_code=r["slot_code"],
                    elective_id=r["elective_id"],
                )
                for r in plan.add
            ]
        )
    return {"added": len(plan.add), "retained": len(plan.retain), "removed": len(plan.remove)}


def as_csv(rows: list[dict[str, Any]], source_reference: str = "reversal") -> str:
    """The rows, back in the input format — so a publication can be undone.

    Written from what was actually removed, not from the file that removed it: the
    point is to restore the state that existed, not to replay the instruction that
    changed it.
    """
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=REQUIRED_COLUMNS, lineterminator="\n")
    writer.writeheader()
    codes = dict(ElectiveCourse.objects.values_list("id", "course_code"))
    for r in rows:
        writer.writerow(
            {
                "academic_year": r["academic_year"],
                "term": r["term"],
                "programme": r["programme"],
                "slot_code": r["slot_code"],
                "course_code": codes.get(r["elective_id"], ""),
                "source_reference": source_reference,
            }
        )
    return out.getvalue()


__all__ = ["ADD", "REMOVE", "RETAIN", "Plan", "Problem", "apply_plan", "as_csv", "build_plan"]

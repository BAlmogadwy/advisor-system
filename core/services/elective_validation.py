"""Term-scoped elective publication and selection rules.

These rules govern new choices. Historical academic evidence has a separate
reader in academic_state and is deliberately not rewritten by this service.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from typing import Any

from django.db import connection, transaction

from core.models import (
    ElectiveCourse,
    ElectiveMappingScope,
    ElectiveTermMapping,
    ProgrammeRequirement,
)
from core.services.student_helpers import is_elective_slot, normalize_code


@dataclass(frozen=True)
class MappingProblem:
    code: str
    message: str
    row: int | None = None


class ElectiveMappingError(ValueError):
    def __init__(self, problems: list[MappingProblem]) -> None:
        self.problems = problems
        super().__init__("; ".join(problem.message for problem in problems))

    def as_dict(self) -> dict[str, Any]:
        return {"ok": False, "error": str(self), "errors": [asdict(p) for p in self.problems]}


def programme_variants(program: str) -> tuple[str, ...]:
    """Keep the supported curriculum-version fallback (for example AI2 -> AI)."""
    program = normalize_code(program)
    if program.endswith("2") and len(program) > 1:
        return program, program[:-1]
    return (program,)


def validate_scope(year: Any, term: Any, program: Any) -> tuple[str, int, str]:
    problems = []
    year = str(year).strip() if isinstance(year, str | int) and not isinstance(year, bool) else ""
    term_text = (
        str(term).strip() if isinstance(term, str | int) and not isinstance(term, bool) else ""
    )
    program = normalize_code(program) if isinstance(program, str) else ""
    if len(year) != 4 or not year.isascii() or not year.isdigit():
        problems.append(MappingProblem("BAD_YEAR", "academic_year must contain four digits"))
    if term_text not in {"1", "2", "3"}:
        problems.append(MappingProblem("BAD_TERM", "term must be 1, 2 or 3"))
    if not program:
        problems.append(MappingProblem("BAD_PROGRAMME", "programme is required"))
    if problems:
        raise ElectiveMappingError(problems)
    return year, int(term_text), program


def option_problems(
    program: str, slot_credits: int | None, options: list[dict[str, Any]]
) -> list[str]:
    """A missing/zero credit value cannot establish compatibility for a new choice."""
    problems = []
    allowed = programme_variants(program)
    unowned = sorted({o["course_code"] for o in options if not normalize_code(o["programme"])})
    foreign = sorted(
        {
            normalize_code(o["programme"])
            for o in options
            if normalize_code(o["programme"]) and normalize_code(o["programme"]) not in allowed
        }
    )
    if unowned:
        problems.append(f"catalogue entry has no programme: {', '.join(unowned[:3])}")
    if foreign:
        problems.append(f"cross-programme mapping from {', '.join(foreign)}")
    if not slot_credits or slot_credits < 0:
        problems.append("slot credits must be a verified positive integer")
    unknown = sorted(
        {o["course_code"] for o in options if not o["credit_hours"] or o["credit_hours"] < 0}
    )
    if unknown:
        problems.append(
            f"course credits must be a verified positive integer: {', '.join(unknown[:3])}"
        )
    wrong = sorted(
        {
            o["course_code"]
            for o in options
            if slot_credits and o["credit_hours"] and o["credit_hours"] != slot_credits
        }
    )
    if wrong:
        problems.append(f"credit mismatch (slot {slot_credits}h): {', '.join(wrong[:3])}")
    return problems


class ElectiveSelection:
    """One fresh, bounded set of catalogue/requirements/mappings for a request.

    Canonical matching happens in Python because the legacy TextFields allow
    whitespace and case variants. Never let a dict silently pick one ambiguous
    identity. This is three queries for all slots, rather than three per slot.
    """

    def __init__(self, program: str, year: str, term: int | str) -> None:
        self.year, self.term, self.program = validate_scope(year, term, program)
        self.variants = programme_variants(self.program)
        self.requirements: dict[str, list[dict]] = defaultdict(list)
        for row in ProgrammeRequirement.objects.values(
            "id", "program", "course_code", "type", "credit_hours"
        ):
            if normalize_code(row["program"]) == self.program:
                self.requirements[normalize_code(row["course_code"])].append(row)
        self.catalogue: dict[tuple[str, str], list[dict]] = defaultdict(list)
        self.by_id = {}
        for row in ElectiveCourse.objects.values(
            "id", "programme", "course_code", "course_name", "credit_hours", "prerequisites_csv"
        ):
            self.by_id[row["id"]] = row
            self.catalogue[
                (normalize_code(row["programme"]), normalize_code(row["course_code"]))
            ].append(row)
        self.mappings: dict[str, list[dict]] = defaultdict(list)
        for row in ElectiveTermMapping.objects.filter(
            academic_year=self.year, term=self.term
        ).values("id", "programme", "placeholder_code", "elective_id"):
            if normalize_code(row["programme"]) in self.variants:
                self.mappings[normalize_code(row["placeholder_code"])].append(row)

    def requirement(self, code: str) -> tuple[dict | None, list[str]]:
        rows = self.requirements.get(normalize_code(code), [])
        if len(rows) > 1:
            return None, ["ambiguous canonical placeholder identity"]
        if not rows or not is_elective_slot(rows[0]["type"]):
            return None, ["not an elective slot for this programme"]
        return rows[0], []

    def catalogue_option(self, code: str) -> tuple[dict | None, list[str]]:
        code = normalize_code(code)
        for owner in self.variants:
            rows = self.catalogue.get((owner, code), [])
            if len(rows) > 1:
                return None, ["ambiguous canonical catalogue identity"]
            if rows:
                return rows[0], []
        if any(key[1] == code for key in self.catalogue):
            return None, ["course has no compatible catalogue owner"]
        return None, ["course not found in catalogue"]

    def resolve(self, slot: str) -> tuple[str, list[dict[str, Any]], list[str]]:
        slot = normalize_code(slot)
        requirement, problems = self.requirement(slot)
        if requirement is None:
            return (
                (
                    "INVALID_MAPPING"
                    if self.requirements.get(slot) and len(self.requirements[slot]) > 1
                    else "NOT_PUBLISHED"
                ),
                [],
                problems,
            )
        mappings = self.mappings.get(slot, [])
        if not mappings:
            return "NOT_PUBLISHED", [], []
        options = []
        seen: dict[str, tuple[str, int]] = {}
        # Exact-programme publication wins when both it and the base publication
        # name one course; the other base-programme options remain available.
        for mapping in sorted(
            mappings, key=lambda row: (normalize_code(row["programme"]) != self.program, row["id"])
        ):
            option = self.by_id.get(mapping["elective_id"])
            if option is None:
                problems.append("mapping resolves to no course")
                continue
            code = normalize_code(option["course_code"])
            if not code:
                problems.append("catalogue course code is missing")
                continue
            mapping_owner = normalize_code(mapping["programme"])
            if code in seen:
                previous_owner, previous_id = seen[code]
                if previous_owner == mapping_owner and previous_id != option["id"]:
                    problems.append("ambiguous normalized option in one publication")
                continue
            seen[code] = (mapping_owner, option["id"])
            identity = (normalize_code(option["programme"]), code)
            if len(self.catalogue[identity]) != 1:
                problems.append("ambiguous canonical catalogue identity")
            options.append({**option, "course_code": code})
        problems.extend(option_problems(self.program, requirement["credit_hours"], options))
        if problems:
            return "INVALID_MAPPING", [], list(dict.fromkeys(problems))
        return "READY", sorted(options, key=lambda row: row["course_code"]), []

    def replacement(self, mappings: Any) -> list[tuple[str, int]]:
        if not isinstance(mappings, list):
            raise ElectiveMappingError(
                [MappingProblem("BAD_MAPPINGS", "mappings must be an explicit array")]
            )
        if not self.requirements:
            raise ElectiveMappingError(
                [MappingProblem("UNKNOWN_PROGRAMME", "programme has no declared requirements")]
            )
        problems = []
        planned = []
        seen = set()
        for index, row in enumerate(mappings, 1):
            if (
                not isinstance(row, dict)
                or not isinstance(row.get("placeholder_code"), str)
                or not isinstance(row.get("course_code"), str)
            ):
                problems.append(
                    MappingProblem(
                        "BAD_ROW",
                        "each mapping needs placeholder_code and course_code strings",
                        index,
                    )
                )
                continue
            slot, code = normalize_code(row["placeholder_code"]), normalize_code(row["course_code"])
            if not slot or not code:
                problems.append(
                    MappingProblem(
                        "BAD_ROW", "placeholder_code and course_code must be nonempty", index
                    )
                )
                continue
            if (slot, code) in seen:
                problems.append(MappingProblem("DUPLICATE", "duplicate normalized mapping", index))
                continue
            seen.add((slot, code))
            requirement, slot_errors = self.requirement(slot)
            option, course_errors = self.catalogue_option(code)
            errors = slot_errors + course_errors
            if requirement is not None and option is not None:
                errors.extend(option_problems(self.program, requirement["credit_hours"], [option]))
            if errors:
                problems.extend(
                    MappingProblem("INVALID_MAPPING", f"{slot} -> {code}: {error}", index)
                    for error in errors
                )
            else:
                planned.append((slot, option["id"]))
        if problems:
            raise ElectiveMappingError(problems)
        return planned


def _lock_validation_inputs() -> None:
    if connection.vendor == "postgresql":
        # A row lock cannot prevent insertion of a conflicting canonical alias.
        # These infrequent admin replacements hold shared table locks while
        # checking the catalogue: other readers still run, catalogue writers
        # wait until commit. SQLite's configured IMMEDIATE transaction supplies
        # the corresponding writer exclusion.
        tables = ", ".join(
            connection.ops.quote_name(model._meta.db_table)
            for model in (ElectiveCourse, ProgrammeRequirement)
        )
        with connection.cursor() as cursor:
            cursor.execute(f"LOCK TABLE {tables} IN SHARE MODE")


def replace_mappings(year: Any, term: Any, program: Any, mappings: Any) -> dict[str, Any]:
    year, term, program = validate_scope(year, term, program)
    if not isinstance(mappings, list):
        raise ElectiveMappingError(
            [MappingProblem("BAD_MAPPINGS", "mappings must be an explicit array")]
        )
    with transaction.atomic():
        scope, _ = ElectiveMappingScope.objects.get_or_create(
            academic_year=year, term=term, programme=program
        )
        ElectiveMappingScope.objects.select_for_update().get(pk=scope.pk)
        _lock_validation_inputs()
        selection = ElectiveSelection(program, year, term)
        planned = selection.replacement(mappings)
        current = [
            row
            for rows in selection.mappings.values()
            for row in rows
            if normalize_code(row["programme"]) == program
        ]
        current_ids = [row["id"] for row in current]
        old = {(normalize_code(row["placeholder_code"]), row["elective_id"]) for row in current}
        new = set(planned)
        unchanged = old == new and len(current) == len(new)
        if not unchanged:
            ElectiveTermMapping.objects.filter(id__in=current_ids).delete()
            ElectiveTermMapping.objects.bulk_create(
                [
                    ElectiveTermMapping(
                        academic_year=year,
                        term=term,
                        programme=program,
                        placeholder_code=slot,
                        elective_id=elective_id,
                    )
                    for slot, elective_id in planned
                ]
            )
        from core.services.reporting import clear_aggregate_cache

        transaction.on_commit(clear_aggregate_cache)
        return {
            "ok": True,
            "programme": program,
            "academic_year": year,
            "term": term,
            "cleared": 0 if unchanged else len(current),
            "created": 0 if unchanged else len(planned),
            "retained": len(planned) if unchanged else 0,
            "total": len(planned),
            "errors": [],
        }

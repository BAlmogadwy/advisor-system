"""
core/services/section_limits.py
Section Planning's write path for per-course seat limits
(``ProgrammeRequirement.max_capacity``). DB Admin's "Course capacities" panel
(``db_admin_views.db_update_programme_capacities_view``) is a second, older
path for the same column that does not go through this module.

A seat limit belongs to a PROGRAMME. Saving from Section Planning writes it only
for the programmes on screen; one course may be widened, explicitly, to every
programme that teaches it. Nothing is written without a preview: the caller
first asks for the exact rows that would change (course, programme, old -> new),
shows them, and then commits with the token of that preview. If anything moved
in between, the commit is refused rather than writing something nobody saw.

Every changed row is recorded with ``record_audit_event`` inside the same
transaction as the write, so a failed audit write rolls the save back: the
change and its record exist together or not at all. That transaction is an
``audited_transaction``: it commits while holding the process audit lock, so
another request's audit row can neither deadlock with it nor fork the chain.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from core.models import ProgrammeRequirement
from core.services.audit import audited_transaction
from core.services.course_identity import normalize_course_name
from core.services.student_helpers import normalize_code

LIMIT_MIN = 1
LIMIT_MAX = 500
MAX_CHANGES = 300
MAX_PROGRAMS = 20
AUDIT_ACTION = "section_planning.limit_change"

SCOPE_PROGRAMMES = "programmes"
SCOPE_ALL = "all_programmes"


class LimitRequestError(ValueError):
    """A refused request, with a stable code the page translates."""

    def __init__(self, code: str, message: str, *, status: int = 400, **fields: Any) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.fields = fields

    def as_dict(self) -> dict[str, Any]:
        return {"ok": False, "code": self.code, "error": str(self), **self.fields}


class LimitConflict(RuntimeError):
    """A row no longer holds the value the confirmed preview showed."""


@dataclass(frozen=True)
class LimitChange:
    """One requested change: a course's new limit (``None`` removes it)."""

    course_code: str
    max_capacity: int | None
    all_programmes: bool


@dataclass(frozen=True)
class PlannedWrite:
    """One row that WILL change, and exactly how."""

    requirement_id: int
    program: str
    course_code: str
    old: int | None
    new: int | None
    scope: str
    course_name: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "course_code": self.course_code,
            "course_name": self.course_name,
            "program": self.program,
            "old": self.old,
            "new": self.new,
            "scope": self.scope,
        }


def _limit_value(raw: object, course_code: str) -> int | None:
    if raw is None:
        return None
    # bool is an int in Python; a checkbox value must never become a seat limit.
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise LimitRequestError(
            "invalid_limit",
            f"The limit for {course_code} must be a whole number from {LIMIT_MIN} to {LIMIT_MAX}.",
            course_code=course_code,
        )
    if not LIMIT_MIN <= raw <= LIMIT_MAX:
        raise LimitRequestError(
            "invalid_limit",
            f"The limit for {course_code} must be a whole number from {LIMIT_MIN} to {LIMIT_MAX}.",
            course_code=course_code,
        )
    return raw


def known_programmes() -> dict[str, str]:
    """Normalised programme code -> the spelling stored on its requirement rows."""
    stored: dict[str, str] = {}
    for program in ProgrammeRequirement.objects.values_list("program", flat=True).distinct():
        norm = normalize_code(program)
        if norm:
            stored.setdefault(norm, str(program))
    return stored


def parse_limit_request(body: object) -> tuple[list[str], list[LimitChange]]:
    """Validate a save request; returns (programmes on screen, requested changes)."""
    if not isinstance(body, dict):
        raise LimitRequestError("invalid_json", "The request body must be a JSON object.")

    raw_programs = body.get("programs")
    if not isinstance(raw_programs, list) or not raw_programs:
        raise LimitRequestError(
            "programs_required", "Choose a programme first: limits are saved per programme."
        )
    if len(raw_programs) > MAX_PROGRAMS:
        raise LimitRequestError("programs_required", "Too many programmes in one save.")
    stored = known_programmes()
    programs: list[str] = []
    for raw in raw_programs:
        norm = normalize_code(raw) if isinstance(raw, str) else ""
        if not norm or norm not in stored:
            raise LimitRequestError(
                "unknown_program", f"Unknown programme: {raw}.", program=str(raw)
            )
        if stored[norm] not in programs:
            programs.append(stored[norm])

    raw_changes = body.get("changes")
    if not isinstance(raw_changes, list) or not raw_changes:
        raise LimitRequestError("changes_required", "There is nothing to save.")
    if len(raw_changes) > MAX_CHANGES:
        raise LimitRequestError(
            "too_many_changes",
            f"Save at most {MAX_CHANGES} courses at a time.",
            max=MAX_CHANGES,
        )
    changes: list[LimitChange] = []
    seen: set[str] = set()
    for raw in raw_changes:
        if not isinstance(raw, dict) or not isinstance(raw.get("course_code"), str):
            raise LimitRequestError("invalid_course", "A change is missing its course code.")
        code = normalize_code(raw["course_code"])
        if not code:
            raise LimitRequestError("invalid_course", "A change is missing its course code.")
        if code in seen:
            raise LimitRequestError(
                "duplicate_course", f"{code} appears twice in one save.", course_code=code
            )
        seen.add(code)
        if "max_capacity" not in raw:
            raise LimitRequestError(
                "invalid_limit",
                f"The limit for {code} must be a whole number from {LIMIT_MIN} to {LIMIT_MAX}.",
                course_code=code,
            )
        all_programmes = raw.get("all_programmes", False)
        if not isinstance(all_programmes, bool):
            raise LimitRequestError(
                "invalid_scope",
                f"The scope for {code} must be true or false.",
                course_code=code,
            )
        changes.append(
            LimitChange(
                course_code=code,
                max_capacity=_limit_value(raw.get("max_capacity"), code),
                all_programmes=all_programmes,
            )
        )
    return programs, changes


def plan_limit_writes(
    programs: list[str], changes: Iterable[LimitChange]
) -> tuple[list[PlannedWrite], int]:
    """The rows that would change, and how many targeted rows already match.

    Scope is the programmes on screen. A change flagged ``all_programmes``
    widens THAT course to every programme that teaches it, and only that one:
    a row elsewhere that shares the code but names a different course (AI492
    is a graduation project in AI and co-op training in AI2) is not the same
    course and is left alone. A course the programmes on screen do not teach
    is refused: the page only offers courses it shows, so anything else is a
    stale or forged request. Each planned row carries its own course name, so
    the confirmation shows what every row is.
    """
    changes = list(changes)
    codes = sorted({change.course_code for change in changes})
    rows_by_code: dict[str, list[dict[str, Any]]] = {}
    for row in ProgrammeRequirement.objects.filter(course_code__in=codes).values(
        "id", "program", "course_code", "course_name", "max_capacity"
    ):
        rows_by_code.setdefault(normalize_code(row["course_code"]), []).append(row)

    on_screen = set(programs)
    writes: list[PlannedWrite] = []
    unchanged = 0
    for change in changes:
        rows = rows_by_code.get(change.course_code, [])
        if not any(row["program"] in on_screen for row in rows):
            raise LimitRequestError(
                "course_not_in_programmes",
                f"{change.course_code} is not taught by {', '.join(programs)}.",
                course_code=change.course_code,
            )
        scope = SCOPE_ALL if change.all_programmes else SCOPE_PROGRAMMES
        shown = [r for r in rows if r["program"] in on_screen]
        targets = [r for r in rows if _same_course(r, shown)] if change.all_programmes else shown
        for row in sorted(targets, key=lambda r: (str(r["program"]), int(r["id"]))):
            old = row["max_capacity"]
            if old == change.max_capacity:
                unchanged += 1
                continue
            writes.append(
                PlannedWrite(
                    requirement_id=int(row["id"]),
                    program=str(row["program"]),
                    course_code=change.course_code,
                    old=old,
                    new=change.max_capacity,
                    scope=scope,
                    course_name=str(row.get("course_name") or "").strip(),
                )
            )
    writes.sort(key=lambda w: (w.course_code, w.program, w.requirement_id))
    return writes, unchanged


def _same_course(row: dict[str, Any], shown: list[dict[str, Any]]) -> bool:
    """Is ``row`` the course a programme on screen teaches under the same code?

    Same code and same name (``planner_course_key``'s reading of a name). A row
    with no name cannot be told apart, so it counts as the same course; the
    confirmation still names every row.
    """
    name = _name_of(row)
    for other in shown:
        other_name = _name_of(other)
        if not name or not other_name or name == other_name:
            return True
    return False


def _name_of(row: dict[str, Any]) -> str:
    """A row's course name as ``planner_course_key`` reads it ("" when it is only the code)."""
    name = normalize_course_name(row.get("course_name"))
    return "" if name == normalize_code(row.get("course_code")) else name


def preview_token(programs: list[str], writes: Iterable[PlannedWrite]) -> str:
    """A fingerprint of exactly what a preview showed; the commit must match it."""
    canonical = json.dumps(
        {
            "programs": sorted(programs),
            "writes": [
                [w.requirement_id, w.program, w.course_code, w.old, w.new, w.scope] for w in writes
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def apply_limit_writes(
    writes: list[PlannedWrite],
    *,
    audit: Callable[[PlannedWrite, int, int], object],
) -> None:
    """Write every planned row and its audit record, all or nothing.

    Each update is conditional on the row still holding the previewed value, so
    a concurrent change raises ``LimitConflict`` instead of being overwritten.
    ``audit`` runs inside the same transaction after each row; whatever it
    raises rolls every row of this save back. The transaction holds the
    process audit lock until it has committed (``audited_transaction``).
    """
    total = len(writes)
    with audited_transaction():
        for position, write in enumerate(writes, 1):
            rows = ProgrammeRequirement.objects.filter(pk=write.requirement_id)
            if write.old is None:
                rows = rows.filter(max_capacity__isnull=True)
            else:
                rows = rows.filter(max_capacity=write.old)
            if rows.update(max_capacity=write.new) != 1:
                raise LimitConflict(
                    f"{write.program} {write.course_code} changed after it was reviewed."
                )
            audit(write, position, total)

"""
core/services/programme_meeting_pattern_editor.py
Section Planning's editor for a programme's weekly meetings: the one write path
for ``ProgrammeRequirement.meeting_pattern``.

Each programme votes on how a 3-credit course of its study plan meets each week:
``""`` not set, ``"standard"`` (2 x 75 min, chosen on purpose) or ``"2x100"``
(owner, 2026-11-10: set per programme, Male and Female alike). The rule that
turns the votes into a scenario's course lives in ``timetable_meeting_shape``;
which rows may vote, the scenario resolver and the words live in
``programme_meeting_patterns``. This module lists the votes of the programmes on
screen and saves them, with the protections of the seat-limit editor
(``section_limits``, whose pattern it follows and which it leaves as it is):

* One entry per course IDENTITY (CODE::NAME), never per bare code. MATH243
  LINEAR ALGEBRA I and CYP2's MATH243 LINEAR ALGEBRA are two courses: each has
  its own row, and each names the other, so it can be set on its own row.
* A save writes the programmes on screen. One course may be widened,
  explicitly, to every programme with a row of that exact identity.
* A value is set only on a row that may vote (``ineligibility``); any other row
  is skipped and named in the preview. Clearing is always allowed, so a row
  that stopped being offered (renamed, re-typed, excluded) can still be cleared.
* Nothing is written without a preview: the commit carries the preview's token
  (a SHA-256 of exactly the rows, old -> new), and each row is written only if
  it still holds the previewed value, name and credit hours; otherwise nothing
  is saved (409).
* Every changed row is recorded with a strict ``record_audit_event`` inside one
  ``audited_transaction``: no audit row, no change (503).

The preview also says what the change means elsewhere: the other programmes that
teach the course and keep their own value (under the unanimity rule a programme
that chose Standard keeps a shared course at 2 x 75), and the saved draft
scenarios whose budget for the course now differs from what their programmes
decide. Saved scenarios never change here: each keeps its own copy until
someone uses the programme default in the timetable workspace.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from core.models import ProgrammeRequirement, ScenarioSectionBudget, TimetableScenario
from core.services.audit import AuditUnavailable, audited_transaction, record_audit_event
from core.services.programme_meeting_patterns import (
    NOT_THREE_CREDITS,
    ROW_FIELDS,
    ineligibility,
    is_drift,
    normalise_key,
    pattern_choice_options,
    pre_key,
    resolve_for_keys,
    row_identity,
    scenarios_programmes,
)
from core.services.section_limits import SCOPE_ALL, SCOPE_PROGRAMMES
from core.services.student_helpers import normalize_code
from core.services.timetable_course_policy import get_course_meeting_policy
from core.services.timetable_meeting_shape import (
    AGREE,
    MIXED,
    NO_VOTE,
    PROGRAMME_NOT_SET,
    PROGRAMME_PATTERN_CHOICES,
    STANDARD,
    ProgrammeRow,
    contributing_rows,
    pattern_choices,
    shape_for,
    validate_programme_meeting_pattern,
)

logger = logging.getLogger(__name__)

#: The audit action of every saved row, and the name the preview token carries.
AUDIT_ACTION = "section_planning.meeting_pattern_change"
MAX_CHANGES = 300
MAX_PROGRAMS = 20
#: The most draft scenarios one previewed course names: the newest, newest first
#: (the preview says how many more it left out).
MAX_SCENARIOS = 50

#: The study-plan columns the editor reads: the rule's, and the row's id.
EDITOR_FIELDS = ("id", *ROW_FIELDS)


class PatternRequestError(ValueError):
    """A refused request, with a stable code the page translates."""

    def __init__(self, code: str, message: str, *, status: int = 400, **fields: Any) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.fields = fields

    def as_dict(self) -> dict[str, Any]:
        return {"ok": False, "code": self.code, "error": str(self), **self.fields}


class PatternConflict(RuntimeError):
    """A row no longer holds the value, name or credit hours the preview showed."""


@dataclass(frozen=True)
class PatternChange:
    """One requested change: a course's new vote (``""`` clears it)."""

    course_key: str
    meeting_pattern: str
    all_programmes: bool


@dataclass(frozen=True)
class PlannedPatternWrite:
    """One row that WILL change, and exactly how.

    ``course_name`` and ``credit_hours`` are the row's as stored: the commit
    writes the row only while it still holds them.
    """

    requirement_id: int
    program: str
    course_key: str
    course_code: str
    course_name: str
    credit_hours: int | None
    old: str
    new: str
    scope: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "course_key": self.course_key,
            "course_code": self.course_code,
            "course_name": self.course_name.strip(),
            "program": self.program,
            "credit_hours": self.credit_hours,
            "old": self.old,
            "new": self.new,
            "scope": self.scope,
        }


@dataclass(frozen=True)
class SkippedRow:
    """A targeted row a value cannot be set on, and why (an ineligibility code)."""

    program: str
    course_key: str
    course_code: str
    course_name: str
    meeting_pattern: str
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "course_key": self.course_key,
            "course_code": self.course_code,
            "course_name": self.course_name,
            "program": self.program,
            "meeting_pattern": self.meeting_pattern,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class PatternPlan:
    """What a request would do: the writes, the skipped rows, the rows already
    holding the value, and which programmes each course reached."""

    writes: list[PlannedPatternWrite]
    skipped: list[SkippedRow]
    unchanged: int
    reached: dict[str, set[str]]


@dataclass(frozen=True)
class EditorActor:
    """Who saves, and through which request: every audit row of the save records it."""

    username: str
    role: str
    endpoint: str
    method: str


@dataclass(frozen=True)
class PatternSaveResult:
    """The answer to one preview or commit: an HTTP status and its JSON body."""

    status: int
    body: dict[str, Any]


# ---------------------------------------------------------------------------
# Reading the study plan
# ---------------------------------------------------------------------------


def _all_rows() -> list[dict[str, Any]]:
    """Every study-plan row, with what the editor reads (one query).

    The whole table, a few hundred rows: a listing needs other programmes' rows
    of the same course (co-owners, other names), and a save its widening.
    """
    return list(
        ProgrammeRequirement.objects.order_by("program", "course_code", "id").values(*EDITOR_FIELDS)
    )


def _stored(row: Mapping[str, Any]) -> str:
    """A row's vote exactly as stored.

    The database keeps it to "", "standard" or "2x100"
    (``ck_pr_meeting_pattern_fits_credits``), so the stored text is also the
    value the commit's conditional update matches.
    """
    return str(row.get("meeting_pattern") or "")


def _name(row: Mapping[str, Any]) -> str:
    return str(row.get("course_name") or "").strip()


def _program(row: Mapping[str, Any]) -> str:
    return str(row.get("program") or "")


def _require_programs(raw_programs: object) -> list[Any]:
    """The programmes a request names, before they are looked up: one to MAX_PROGRAMS."""
    if not isinstance(raw_programs, list) or not raw_programs:
        raise PatternRequestError(
            "programs_required",
            "Choose a programme first: weekly meetings are saved per programme.",
        )
    if len(raw_programs) > MAX_PROGRAMS:
        raise PatternRequestError("programs_required", "Too many programmes at once.")
    return raw_programs


def _programmes_on_screen(raw_programs: object, rows: list[dict[str, Any]]) -> list[str]:
    """The programmes a request names, as their rows spell them; refuses the rest."""
    raw_programs = _require_programs(raw_programs)
    stored: dict[str, str] = {}
    for row in rows:
        norm = normalize_code(row.get("program"))
        if norm:
            stored.setdefault(norm, _program(row))
    programs: list[str] = []
    for raw in raw_programs:
        norm = normalize_code(raw) if isinstance(raw, str) else ""
        if not norm or norm not in stored:
            raise PatternRequestError(
                "unknown_program", f"Unknown programme: {raw}.", program=str(raw)
            )
        if stored[norm] not in programs:
            programs.append(stored[norm])
    return programs


def _vote_status(values: Iterable[str]) -> tuple[str, str]:
    """How stored votes come out: (status, the agreed value or "").

    The editor shows what the rows HOLD. For a row that may vote that is its
    vote, so this is ``merge_programme_patterns``'s outcome; for a clear-only
    row (no name, say, which never votes) it is the value still to clear.
    """
    votes = {value for value in values if value != PROGRAMME_NOT_SET}
    if not votes:
        return NO_VOTE, PROGRAMME_NOT_SET
    if len(votes) > 1:
        return MIXED, PROGRAMME_NOT_SET
    return AGREE, votes.pop()


class _Catalogue:
    """The study plan grouped the ways the editor reads it (no queries)."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.reason = {int(row["id"]): ineligibility(row) for row in rows}
        self.by_identity: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.by_code: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(
            lambda: defaultdict(list)
        )
        for row in rows:
            key = row_identity(row)
            self.by_identity[key].append(row)
            self.by_code[key.split("::", 1)[0]][key].append(row)

    def eligible(self, row: Mapping[str, Any]) -> bool:
        return self.reason[int(row["id"])] is None

    def co_owners(self, key: str, reached: set[str]) -> list[dict[str, str]]:
        """Programmes outside ``reached`` with a row of ``key`` that may vote."""
        return [
            {"program": _program(row), "meeting_pattern": _stored(row)}
            for row in sorted(self.by_identity.get(key, []), key=_program)
            if _program(row) not in reached and self.eligible(row)
        ]

    def same_code_elsewhere(self, key: str) -> list[dict[str, Any]]:
        """The other courses planned under this code, each settable on its own row.

        Only courses with a row that may vote: the page tells the user to set
        that row, so a course that cannot take a value is not named.
        """
        out = []
        for other, group in sorted(self.by_code.get(key.split("::", 1)[0], {}).items()):
            owners = [row for row in group if self.eligible(row)]
            if other == key or not owners:
                continue
            out.append(
                {
                    "course_key": other,
                    "course_name": _name(owners[0]),
                    "programmes": sorted({_program(row) for row in owners}),
                }
            )
        return out


# ---------------------------------------------------------------------------
# The listing (GET)
# ---------------------------------------------------------------------------


def meeting_pattern_rows(programs: Iterable[str]) -> dict[str, Any]:
    """The weekly-meetings votes of the programmes on screen, one entry per course.

    A course is listed when a programme on screen has a row of it that may vote,
    or a row that still holds a vote although it may no longer vote (flagged
    ``clear_only``, with the reason: such a value can only be cleared). Each
    entry::

        {course_key, course_code, course_name, credit_hours,
         programmes: [{program, meeting_pattern}],   # the rows on screen
         status, meeting_pattern,                    # how they come out
         co_owners: [{program, meeting_pattern}],    # other programmes' rows
         same_code_elsewhere: [{course_key, course_name, programmes}],
         clear_only, reason}

    ``choices`` are the three votes with their words; the standard minutes are
    the engine's (``MEETING_PATTERNS``), never the page's. No programme: nothing
    is read. An unknown programme is refused (``PatternRequestError``).
    """
    wanted = [str(p).strip() for p in programs if str(p).strip()]
    choices = pattern_choice_options(3)
    if not wanted:
        return {"programs": [], "courses": [], "choices": choices}
    rows = _all_rows()
    on_screen = _programmes_on_screen(wanted, rows)
    shown = set(on_screen)
    catalogue = _Catalogue(rows)

    courses = []
    for key, group in catalogue.by_identity.items():
        mine = sorted(
            (
                row
                for row in group
                if _program(row) in shown and (catalogue.eligible(row) or _stored(row))
            ),
            key=_program,
        )
        if not mine:
            continue
        voters = [row for row in mine if catalogue.eligible(row)]
        status, agreed = _vote_status(_stored(row) for row in mine)
        first = (voters or mine)[0]
        courses.append(
            {
                "course_key": key,
                "course_code": key.split("::", 1)[0],
                "course_name": _name(first),
                "credit_hours": first.get("credit_hours"),
                "programmes": [
                    {"program": _program(row), "meeting_pattern": _stored(row)} for row in mine
                ],
                "status": status,
                "meeting_pattern": agreed,
                "co_owners": catalogue.co_owners(key, shown),
                "same_code_elsewhere": catalogue.same_code_elsewhere(key),
                "clear_only": not voters,
                "reason": None if voters else catalogue.reason[int(first["id"])],
            }
        )
    courses.sort(key=lambda c: (c["course_code"], c["course_name"], c["course_key"]))
    return {"programs": on_screen, "courses": courses, "choices": choices}


# ---------------------------------------------------------------------------
# A save: request, plan, token
# ---------------------------------------------------------------------------


def _pattern_value(raw: object, key: str) -> str:
    """A requested vote as stored: "", "standard" or "2x100"; refuses anything else."""
    value = raw.strip().lower().replace(" ", "") if isinstance(raw, str) else None
    if value not in PROGRAMME_PATTERN_CHOICES:
        raise PatternRequestError(
            "invalid_pattern",
            f"The weekly meetings for {key} must be not set, standard or 2x100.",
            course_key=key,
        )
    return value


def parse_pattern_changes(body: Mapping[str, Any]) -> list[PatternChange]:
    """Validate a save request's changes (the programmes are checked against the plan)."""
    raw_changes = body.get("changes")
    if not isinstance(raw_changes, list) or not raw_changes:
        raise PatternRequestError("changes_required", "There is nothing to save.")
    if len(raw_changes) > MAX_CHANGES:
        raise PatternRequestError(
            "too_many_changes",
            f"Save at most {MAX_CHANGES} courses at a time.",
            max=MAX_CHANGES,
        )
    changes: list[PatternChange] = []
    seen: set[str] = set()
    for raw in raw_changes:
        key = (
            normalise_key(raw["course_key"])
            if isinstance(raw, dict) and isinstance(raw.get("course_key"), str)
            else ""
        )
        if not key:
            raise PatternRequestError("invalid_course", "A change is missing its course.")
        if key in seen:
            raise PatternRequestError(
                "duplicate_course", f"{key} appears twice in one save.", course_key=key
            )
        seen.add(key)
        if "meeting_pattern" not in raw:
            raise PatternRequestError(
                "invalid_pattern",
                f"The weekly meetings for {key} must be not set, standard or 2x100.",
                course_key=key,
            )
        all_programmes = raw.get("all_programmes", False)
        if not isinstance(all_programmes, bool):
            raise PatternRequestError(
                "invalid_scope", f"The scope for {key} must be true or false.", course_key=key
            )
        changes.append(
            PatternChange(
                course_key=key,
                meeting_pattern=_pattern_value(raw["meeting_pattern"], key),
                all_programmes=all_programmes,
            )
        )
    return changes


def _planned(row: Mapping[str, Any], key: str, new: str, scope: str) -> PlannedPatternWrite:
    return PlannedPatternWrite(
        requirement_id=int(row["id"]),
        program=_program(row),
        course_key=key,
        course_code=key.split("::", 1)[0],
        course_name=str(row.get("course_name") or ""),
        credit_hours=row.get("credit_hours"),
        old=_stored(row),
        new=new,
        scope=scope,
    )


def plan_pattern_writes(
    programs: list[str], changes: Iterable[PatternChange], rows: list[dict[str, Any]]
) -> PatternPlan:
    """The rows a request would change, the rows it would skip, and the rows already set.

    Scope is the programmes on screen: their rows of that EXACT identity. A
    change flagged ``all_programmes`` widens that course, and only that one, to
    every programme with a row of the identity; a row elsewhere that shares the
    code but names another course is never reached. A value is set only on a
    row that may vote; any other targeted row is skipped, with its reason.
    Clearing reaches every targeted row that holds a value. A course the
    programmes on screen do not teach is refused: the page offers only the
    courses it lists, so anything else is a stale or forged request.
    """
    catalogue = _Catalogue(rows)
    on_screen = set(programs)
    writes: list[PlannedPatternWrite] = []
    skipped: list[SkippedRow] = []
    reached: dict[str, set[str]] = {}
    unchanged = 0
    for change in changes:
        key = change.course_key
        group = catalogue.by_identity.get(key, [])
        shown = [row for row in group if _program(row) in on_screen]
        if not shown:
            raise PatternRequestError(
                "course_not_in_programmes",
                f"{key} is not taught by {', '.join(programs)}.",
                course_key=key,
                course_code=key.split("::", 1)[0],
            )
        scope = SCOPE_ALL if change.all_programmes else SCOPE_PROGRAMMES
        targets = group if change.all_programmes else shown
        reached[key] = {_program(row) for row in targets}
        for row in sorted(targets, key=lambda r: (_program(r), int(r["id"]))):
            old = _stored(row)
            if change.meeting_pattern == PROGRAMME_NOT_SET:
                if old == PROGRAMME_NOT_SET:
                    unchanged += 1
                else:
                    writes.append(_planned(row, key, PROGRAMME_NOT_SET, scope))
                continue
            reason = catalogue.reason[int(row["id"])]
            if reason is None:
                try:
                    validate_programme_meeting_pattern(
                        change.meeting_pattern, row.get("credit_hours"), course_code=key
                    )
                except ValueError:
                    reason = NOT_THREE_CREDITS
            if reason is not None:
                skipped.append(
                    SkippedRow(
                        program=_program(row),
                        course_key=key,
                        course_code=key.split("::", 1)[0],
                        course_name=_name(row),
                        meeting_pattern=old,
                        reason=reason,
                    )
                )
            elif old == change.meeting_pattern:
                unchanged += 1
            else:
                writes.append(_planned(row, key, change.meeting_pattern, scope))
    writes.sort(key=lambda w: (w.course_code, w.course_key, w.program, w.requirement_id))
    skipped.sort(key=lambda s: (s.course_code, s.course_key, s.program))
    return PatternPlan(writes=writes, skipped=skipped, unchanged=unchanged, reached=reached)


def preview_token(programs: list[str], writes: Iterable[PlannedPatternWrite]) -> str:
    """A fingerprint of exactly what a preview showed; the commit must match it.

    It names the action, so a token of another editor (seat limits) can never
    commit here, and every field of every row: the course, the row, its
    programme, old -> new, its credit hours and name as stored, and the scope.
    """
    canonical = json.dumps(
        {
            "action": AUDIT_ACTION,
            "programs": sorted(programs),
            "writes": [
                [
                    w.course_key,
                    w.requirement_id,
                    w.program,
                    w.old,
                    w.new,
                    w.credit_hours,
                    w.course_name,
                    w.scope,
                ]
                for w in writes
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# What a change means elsewhere (the preview's second half)
# ---------------------------------------------------------------------------


def _budget_course(budget_key: str, rows: list[ProgrammeRow]) -> str:
    """The course identity a scenario's budget key names among these rows.

    An exact identity names itself; a bare code names the one course its rows
    are, when they are one (``contributing_rows``' reading of a bare key).
    """
    key = normalise_key(budget_key)
    if "::" in key:
        return key
    status, contributors = contributing_rows(key, rows)
    identities = {row.identity for row in contributors}
    return identities.pop() if status == AGREE and len(identities) == 1 else key


def _budget_pattern(budget: Mapping[str, Any]) -> tuple[str, list[str]]:
    """A budget row's weekly meetings as the workspace reads it, and its choices."""
    key = budget.get("course_key") or budget.get("course_code")
    policy = get_course_meeting_policy(budget.get("course_code"), key)
    if policy is not None:
        return STANDARD, [STANDARD]
    shape = shape_for(budget.get("meeting_pattern"), budget.get("credit_hours"))
    choices = pattern_choices(budget.get("credit_hours"))
    return (shape.shape_id if shape else STANDARD), choices


def _programme_label(program: object) -> str:
    """A programme as ``scenario_programmes`` names it (stripped, upper-case)."""
    return str(program or "").strip().upper()


class _DraftScenarios:
    """The saved draft scenarios that budget the changed courses, read once.

    At most three queries for any number of courses and scenarios: the budget
    rows of draft scenarios by code (newest scenario first), those scenarios,
    and one board read for every older scenario saved without its programmes
    (``scenarios_programmes``). A published scenario is never named: it cannot
    change any more.
    """

    def __init__(
        self, codes: set[str], rows: list[dict[str, Any]], new_values: dict[int, str]
    ) -> None:
        self.budgets: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for budget in (
            ScenarioSectionBudget.objects.filter(scenario__status="draft", course_code__in=codes)
            .order_by("-scenario_id", "id")
            .values("scenario_id", "course_key", "course_code", "credit_hours", "meeting_pattern")
        ):
            self.budgets[str(budget["course_code"])].append(budget)
        ids = {b["scenario_id"] for group in self.budgets.values() for b in group}
        self.scenarios = {
            s.id: s
            for s in TimetableScenario.objects.filter(id__in=ids).only("id", "name", "programs")
        }
        self.programmes = scenarios_programmes(self.scenarios.values())
        # The study plan as it will read once the change is saved.
        self.after = [
            {**row, "meeting_pattern": new_values.get(int(row["id"]), _stored(row))} for row in rows
        ]

    def reached_by(self, key: str, programs: set[str]) -> tuple[list[dict[str, Any]], int]:
        """The draft scenarios a change to ``key`` reaches, as they would read it,
        newest first: at most ``MAX_SCENARIOS``, and how many more it reaches.

        A scenario is reached when its programmes include one the change
        targets (``programs``: a row already holding the value counts, since a
        timetable generated before that value was set may still differ) and it
        has a budget row for the course. Each says what its budget holds now,
        what its programmes decide with the NEW values (the shared resolver),
        and whether the two differ: the timetable keeps its own copy until
        someone uses the programme default there. The newest are named, since
        they are the timetables being worked on; the rest are only counted.
        """
        out: dict[int, dict[str, Any]] = {}
        more: set[int] = set()
        for budget in self.budgets.get(key.split("::", 1)[0], []):
            sid = budget["scenario_id"]
            scenario = self.scenarios.get(sid)
            theirs = self.programmes.get(sid, [])
            if scenario is None or sid in out or not programs & set(theirs):
                continue
            their_rows = pre_key(
                row for row in self.after if _programme_label(row["program"]) in theirs
            )
            budget_key = str(budget["course_key"] or budget["course_code"])
            if _budget_course(budget_key, their_rows) != key:
                continue
            if len(out) >= MAX_SCENARIOS:
                more.add(sid)
                continue
            decision = resolve_for_keys([budget_key], theirs, rows=their_rows)[budget_key]
            pattern, choices = _budget_pattern(budget)
            out[sid] = {
                "id": sid,
                "name": scenario.name,
                "budget_pattern": pattern,
                "programme_pattern": decision.meeting_pattern,
                "status": decision.status,
                "differs": is_drift(
                    decision, {"meeting_pattern": pattern, "meeting_pattern_choices": choices}
                ),
            }
        return [out[sid] for sid in sorted(out, reverse=True)], len(more)


def _consequences(plan: PatternPlan, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per changed course: who else teaches it, its other names, its draft scenarios."""
    if not plan.writes:
        return []
    catalogue = _Catalogue(rows)
    by_course: dict[str, list[PlannedPatternWrite]] = defaultdict(list)
    for write in plan.writes:
        by_course[write.course_key].append(write)
    drafts = _DraftScenarios(
        {write.course_code for write in plan.writes},
        rows,
        {write.requirement_id: write.new for write in plan.writes},
    )
    out = []
    for key, writes in by_course.items():
        first = writes[0]
        scenarios, more = drafts.reached_by(
            key, {_programme_label(p) for p in plan.reached.get(key, set())}
        )
        out.append(
            {
                "course_key": key,
                "course_code": first.course_code,
                "course_name": first.course_name.strip(),
                "co_owners": catalogue.co_owners(key, plan.reached.get(key, set())),
                "same_code_elsewhere": catalogue.same_code_elsewhere(key),
                "scenarios": scenarios,
                "scenarios_more": more,
            }
        )
    return out


# ---------------------------------------------------------------------------
# The commit
# ---------------------------------------------------------------------------


def apply_pattern_writes(
    writes: list[PlannedPatternWrite],
    *,
    audit: Callable[[PlannedPatternWrite, int, int], object],
) -> None:
    """Write every planned row and its audit record, all or nothing.

    Each update is conditional on the row still holding the previewed value,
    course name and credit hours, so a concurrent change (an import renaming the
    course or changing its credits, another save) raises ``PatternConflict``
    instead of putting a vote on a course nobody reviewed - and never reaches
    the database's credit-hours check. ``audit`` runs inside the same
    transaction after each row; whatever it raises rolls every row back. The
    transaction holds the process audit lock until it has committed
    (``audited_transaction``).
    """
    total = len(writes)
    with audited_transaction():
        for position, write in enumerate(writes, 1):
            matched = ProgrammeRequirement.objects.filter(
                pk=write.requirement_id,
                meeting_pattern=write.old,
                course_name=write.course_name,
                credit_hours=write.credit_hours,
            ).update(meeting_pattern=write.new)
            if matched != 1:
                raise PatternConflict(
                    f"{write.program} {write.course_code} changed after it was reviewed."
                )
            audit(write, position, total)


def save_meeting_patterns(payload: object, *, actor: EditorActor, source: str) -> PatternSaveResult:
    """Preview, or commit, one save request: the whole write path of the editor.

    Request body::

        {
          "programs": ["AI2", "DS2"],                    # the programmes on screen
          "changes": [
            {"course_key": "STAT103::PRINCIPLES_OF_PROBABILITY_AND_STATISTICS",
             "meeting_pattern": "2x100"},
            {"course_key": "MATH243::243_LINEAR_ALGEBRA_I",
             "meeting_pattern": "standard", "all_programmes": true},
            {"course_key": "COE226::DIGITAL_LOGIC_DESIGN", "meeting_pattern": ""}
          ],
          "dry_run": true,                               # preview: nothing is written
          "preview_token": "…"                           # commit: the token of that preview
        }

    A preview answers the rows that would change (course, programme, old ->
    new, scope), the rows skipped and why, how many already hold the value, and
    per changed course its co-owners, its other names and the draft scenarios
    it reaches. A commit is refused (409 ``preview_stale``) unless its token
    matches what the same request would change now, and (409 ``row_changed``)
    if a row moves while it is written. Every changed row is audited in the
    same transaction; if the audit write fails nothing is saved (503).
    """
    if not isinstance(payload, dict):
        error = PatternRequestError("invalid_json", "The request body must be a JSON object.")
        return PatternSaveResult(error.status, error.as_dict())
    try:
        raw_programs = _require_programs(payload.get("programs"))
        rows = _all_rows()
        programs = _programmes_on_screen(raw_programs, rows)
        changes = parse_pattern_changes(payload)
        plan = plan_pattern_writes(programs, changes, rows)
    except PatternRequestError as exc:
        return PatternSaveResult(exc.status, exc.as_dict())

    token = preview_token(programs, plan.writes)
    preview = {
        "programs": programs,
        "changes": [write.as_dict() for write in plan.writes],
        "skipped": [row.as_dict() for row in plan.skipped],
        "unchanged": plan.unchanged,
        "preview_token": token,
    }
    if payload.get("dry_run") is True:
        return PatternSaveResult(
            200,
            {"ok": True, "dry_run": True, **preview, "courses": _consequences(plan, rows)},
        )

    if payload.get("preview_token") != token:
        return PatternSaveResult(
            409,
            {
                "ok": False,
                "code": "preview_stale",
                "error": "The weekly meetings changed since they were reviewed. Review them again.",
                **preview,
            },
        )

    def _audit(write: PlannedPatternWrite, position: int, total: int) -> None:
        record_audit_event(
            actor_username=actor.username,
            actor_role=actor.role,
            action=AUDIT_ACTION,
            endpoint=actor.endpoint,
            method=actor.method,
            status="success",
            details={
                "requirement_id": write.requirement_id,
                "program": write.program,
                "course_code": write.course_code,
                "course_key": write.course_key,
                "old": write.old,
                "new": write.new,
                "scope": write.scope,
                "programs_on_screen": programs,
                "batch": token[:16],
                "position": position,
                "of": total,
                "source": source,
            },
        )

    try:
        apply_pattern_writes(plan.writes, audit=_audit)
    except PatternConflict:
        return PatternSaveResult(
            409,
            {
                "ok": False,
                "code": "row_changed",
                "error": "A course changed while saving. Nothing was saved; review again.",
            },
        )
    except AuditUnavailable:
        return PatternSaveResult(
            503,
            {
                "ok": False,
                "code": "audit_unavailable",
                "error": "Couldn't record the change in the audit log, so nothing was saved.",
            },
        )

    logger.info(
        "save_meeting_patterns: source=%s user=%s programs=%s changed=%d unchanged=%d "
        "skipped=%d batch=%s",
        source,
        actor.username,
        programs,
        len(plan.writes),
        plan.unchanged,
        len(plan.skipped),
        token[:16],
    )
    return PatternSaveResult(
        200,
        {
            "ok": True,
            "dry_run": False,
            "programs": programs,
            "changed": [write.as_dict() for write in plan.writes],
            "changed_count": len(plan.writes),
            "unchanged": plan.unchanged,
            "skipped": [row.as_dict() for row in plan.skipped],
        },
    )

"""The weekly shape a course is taught in, when the department chooses it.

Credit hours give every course its default weekly shape: a 3-credit course meets
twice a week for 75 minutes (``timetable_autoplace.MEETING_PATTERNS``). A
department may instead teach a 3-credit course as TWO 100-MINUTE MEETINGS a week
(owner, 2026-11-10: STAT103 and MATH243 in the hand-made 1448 sheets). This
module names those shapes and decides, from a stored choice and the course's
credit hours, which one applies.

The contract of the one shape there is today, ``"2x100"``:

* two meetings a week, on two different days;
* each in a 100-minute column - the lab-grid TIMES (Lab 1-5), which is where the
  timetable already keeps its 100-minute windows;
* in a LECTURE room. A meeting's room type is decided by credit hours (a lab
  room only for a 4-credit course's 100-minute meeting:
  ``timetable_rooming._room_type_for_credit_duration``), never by this shape;
* an ordinary duration shape for every rule: same-course separation, day
  spread, one teacher per section, rooms. It is NOT a ``CourseMeetingPolicy``
  (``timetable_course_policy``): a policy means a fixed, roomless English bundle
  that siblings may overlap, selected by registrar code.

Where a choice is stored: ``ScenarioSectionBudget.meeting_pattern``, the
scenario's own copy, which every engine of that scenario reads. ``""`` is the
credit default. A course that is not configured behaves exactly as before.

Where it comes from: each programme sets its own courses (owner, 2026-11-10:
"per programme"; both campuses share a programme's value) on its study-plan row,
``ProgrammeRequirement.meeting_pattern``, which holds the programme's VOTE:
``""`` not set (it abstains), ``"standard"`` (the credit hours' pattern, chosen
on purpose) or a shape. A new scenario pools a course over its programmes, and
:func:`merge_programme_patterns` decides it: the programmes that voted must
agree, a programme that did not vote does not count, and a disagreement keeps
the credit default and is reported, never guessed. So does a course its
programmes teach at different credit hours (3 in one plan, 4 in another): one
budget cannot be both, so a vote decides nothing there. Classic Generate copies
the outcome into the scenario's budget (:func:`department_budget_value`); the new
scheduler is handed that copy, or runs the same merge when it plans on its own.

The module is pure - no Django, no database - so the classic engines, the
workspace, the manual importer and the new scheduler can all read one rule.
Read paths never raise: an unknown or unusable stored value means the credit
default. Only the write paths (:func:`validate_meeting_pattern`) refuse.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

STANDARD = ""
TWO_BY_100 = "2x100"

#: A meeting longer than this sits in the 100-minute column family. It is the
#: same split ``timetable_autoplace._generate_meeting_options`` uses to choose a
#: grid, so a shape's durations are grid selectors: a 100-minute meeting takes
#: whatever length the scenario's 100-minute column has.
LONG_MEETING_MINUTES = 75


@dataclass(frozen=True)
class MeetingShape:
    """A weekly shape a department may choose for a course."""

    shape_id: str
    durations: tuple[int, ...]
    credit_hours: int
    label: str
    label_ar: str


SHAPES: dict[str, MeetingShape] = {
    TWO_BY_100: MeetingShape(
        shape_id=TWO_BY_100,
        durations=(100, 100),
        credit_hours=3,
        label="2 × 100 min",
        label_ar="٢ × ١٠٠ دقيقة",
    ),
}

#: Every value a stored ``meeting_pattern`` may hold, in display order.
PATTERN_CHOICES: tuple[str, ...] = (STANDARD, *SHAPES)


def _engine_credits(credit_hours: object) -> int:
    """Credit hours as the engines read them: missing or unreadable means 3."""
    try:
        return int(credit_hours or 3)
    except (TypeError, ValueError):
        return 3


def _exact_credits(credit_hours: object) -> int | None:
    """Credit hours exactly as stored, for write-time checks (None if unreadable)."""
    if isinstance(credit_hours, bool):
        return None
    try:
        return int(str(credit_hours).strip())
    except (TypeError, ValueError):
        return None


def shape_for(meeting_pattern: object, credit_hours: object) -> MeetingShape | None:
    """The configured shape that applies, or None for the credit default.

    Never raises. A value this module does not know, or a shape stored for a
    course whose credit hours it does not fit, reads as the credit default.
    Credit hours are read as the engines read them (missing means 3), so every
    engine of a scenario resolves the same budget row to the same shape.
    """
    shape = SHAPES.get(str(meeting_pattern or "").strip().lower())
    if shape is None or shape.credit_hours != _engine_credits(credit_hours):
        return None
    return shape


def configured_durations(meeting_pattern: object, credit_hours: object) -> list[int] | None:
    """The configured shape's durations as a fresh list, or None for the default."""
    shape = shape_for(meeting_pattern, credit_hours)
    return list(shape.durations) if shape else None


def pattern_choices(credit_hours: object) -> list[str]:
    """The values an advisor may choose for a course with these credit hours."""
    credits = _exact_credits(credit_hours)
    return [STANDARD, *(s.shape_id for s in SHAPES.values() if s.credit_hours == credits)]


def normalise_meeting_pattern(value: object) -> str:
    """A submitted choice as stored; refuses anything that is not a known shape."""
    text = str(value if value is not None else "").strip().lower().replace(" ", "")
    if text in ("", "standard"):
        return STANDARD
    if text in SHAPES:
        return text
    raise ValueError(f"Unknown weekly meeting pattern: {value!r}.")


def validate_meeting_pattern(
    meeting_pattern: object, credit_hours: object, *, course_code: str = ""
) -> str:
    """Check a choice before it is written; returns the value to store.

    Going back to the credit default is always allowed. A shape is allowed only
    for a course with exactly the credit hours it was defined for.
    """
    value = normalise_meeting_pattern(meeting_pattern)
    if value == STANDARD:
        return value
    shape = SHAPES[value]
    credits = _exact_credits(credit_hours)
    if credits != shape.credit_hours:
        name = course_code or "This course"
        shown = credits if credits is not None else "unknown"
        raise ValueError(
            f"{name} has {shown} credit hours; {shape.label} a week is only for "
            f"{shape.credit_hours}-credit courses."
        )
    return value


def shape_label(meeting_pattern: object, credit_hours: object, *, arabic: bool = False) -> str:
    """The configured shape's label, or "" for the credit default."""
    shape = shape_for(meeting_pattern, credit_hours)
    if shape is None:
        return ""
    return shape.label_ar if arabic else shape.label


def meeting_family(minutes: int) -> str:
    """Which column family a meeting of this length sits in: "long" or "short"."""
    return "long" if int(minutes) > LONG_MEETING_MINUTES else "short"


def meetings_match_shape(minutes: Iterable[int], shape: MeetingShape) -> bool:
    """Do these meetings have the shape's column families, one for one?

    Compared as families, not exact lengths: a scenario's 100-minute column may
    be a few minutes longer or shorter, and a meeting placed in it keeps the
    column's own length.
    """
    placed = sorted(meeting_family(m) for m in minutes)
    return placed == sorted(meeting_family(d) for d in shape.durations)


# ---------------------------------------------------------------------------
# A programme's vote, and the scenario's course decided from the votes
# ---------------------------------------------------------------------------

#: ``ProgrammeRequirement.meeting_pattern``: the programme has not chosen.
PROGRAMME_NOT_SET = ""
#: ``ProgrammeRequirement.meeting_pattern``: the credit hours' pattern, chosen.
PROGRAMME_STANDARD = "standard"
#: Every value a programme's row may hold, in display order.
PROGRAMME_PATTERN_CHOICES: tuple[str, ...] = (PROGRAMME_NOT_SET, PROGRAMME_STANDARD, *SHAPES)

#: How the votes for one course in one scenario came out.
AGREE = "agree"
MIXED = "mixed"
NO_VOTE = "none"
AMBIGUOUS = "ambiguous"


def validate_programme_meeting_pattern(
    meeting_pattern: object, credit_hours: object, *, course_code: str = ""
) -> str:
    """Check a programme's vote before it is written; returns the value to store.

    Clearing it (``""``) is always allowed. ``"standard"`` and a shape are votes
    on a choice that exists only at the credit hours a shape was defined for
    (3), so both are refused at any other credit hours.
    """
    text = str(meeting_pattern if meeting_pattern is not None else "").strip().lower()
    text = text.replace(" ", "")
    if text == PROGRAMME_NOT_SET:
        return text
    if text != PROGRAMME_STANDARD and text not in SHAPES:
        raise ValueError(f"Unknown weekly meeting pattern: {meeting_pattern!r}.")
    if text == PROGRAMME_STANDARD:
        allowed = {s.credit_hours for s in SHAPES.values()}
    else:
        allowed = {SHAPES[text].credit_hours}
    credits = _exact_credits(credit_hours)
    if credits not in allowed:
        name = course_code or "This course"
        shown = credits if credits is not None else "unknown"
        wanted = " or ".join(str(c) for c in sorted(allowed))
        raise ValueError(
            f"{name} has {shown} credit hours; its weekly meetings can be chosen only "
            f"for {wanted}-credit courses."
        )
    return text


@dataclass(frozen=True)
class ProgrammeRow:
    """One programme's study-plan row for a course, ready for the merge.

    ``identity`` is ``planner_course_key(code, course_name)`` (CODE::NAME), which
    the Django side computes: this module must not import it. ``named`` is
    False when the row has no course name; such a row cannot say which course
    it means, so it never votes.
    """

    programme: str
    identity: str
    meeting_pattern: str
    credit_hours: object
    named: bool

    @property
    def code(self) -> str:
        return self.identity.split("::", 1)[0]

    @property
    def vote(self) -> str:
        """``""`` (abstains), ``"standard"`` or a shape id this row's credits fit."""
        value = str(self.meeting_pattern or "").strip().lower()
        if not self.named or (value != PROGRAMME_STANDARD and value not in SHAPES):
            return PROGRAMME_NOT_SET
        if value in SHAPES and shape_for(value, self.credit_hours) is None:
            return PROGRAMME_STANDARD
        return value


@dataclass(frozen=True)
class ProgrammeShape:
    """The programmes' decision for one course in one scenario.

    ``meeting_pattern`` is what they agreed on (``"standard"`` or a shape id),
    ``""`` when nobody voted, the votes differ or the rows' credit hours differ
    (both ``MIXED``). ``programmes`` is every
    contributing programme with its vote (``""`` = not set), sorted.
    """

    status: str
    meeting_pattern: str
    programmes: tuple[tuple[str, str], ...]


def contributing_rows(
    key: str, rows: Iterable[ProgrammeRow]
) -> tuple[str, tuple[ProgrammeRow, ...]]:
    """The rows that decide ``key``, and ``AGREE`` or ``AMBIGUOUS``.

    Rows of that exact identity decide it. A bare key (no ``::``) that no row
    has as its identity - a code typed into Add course, say - is read as the
    code: its rows decide it only when they are all one course; two courses
    behind one code (CS112 PROGRAMMING I and II) are never guessed between.
    """
    rows = tuple(rows)
    exact = tuple(r for r in rows if r.identity == key)
    if exact or "::" in key:
        return AGREE, exact
    same_code = tuple(r for r in rows if r.code == key)
    if len({r.identity for r in same_code}) > 1:
        return AMBIGUOUS, ()
    return AGREE, same_code


def merge_programme_patterns(rows: Iterable[ProgrammeRow]) -> ProgrammeShape:
    """Decide a course from its programmes' votes.

    A programme that has not voted abstains. The votes must agree; if they
    differ the status is ``MIXED`` and nothing is chosen (the credit default
    stands, and the caller says why). Row order never matters.

    The rows must also teach the course at ONE number of credit hours. When
    they do not (AI teaches it at 3 credits and voted 2 x 100, DS2 at 4, where
    no vote is possible) and anyone voted, the status is ``MIXED`` too: one
    scenario budget cannot be both, and the engines size it differently
    (classic Generate from whichever student it meets first, the scheduler from
    the largest), so a shape would depend on student order. The credit default
    stands and the course is reported, never guessed. Credit hours are read as
    the engines read them (missing means 3), over every row given, named or not.
    """
    rows = tuple(rows)
    programmes = tuple(sorted({(str(r.programme).strip().upper(), r.vote) for r in rows}))
    votes = {r.vote for r in rows} - {PROGRAMME_NOT_SET}
    if not votes:
        return ProgrammeShape(NO_VOTE, PROGRAMME_NOT_SET, programmes)
    credits = {_engine_credits(r.credit_hours) for r in rows}
    if len(votes) > 1 or len(credits) > 1:
        return ProgrammeShape(MIXED, PROGRAMME_NOT_SET, programmes)
    return ProgrammeShape(AGREE, votes.pop(), programmes)


def programme_shape_for(key: str, rows: Iterable[ProgrammeRow]) -> ProgrammeShape:
    """The programmes' decision for one scenario course (``key``) from ``rows``."""
    status, contributors = contributing_rows(key, rows)
    if status == AMBIGUOUS:
        return ProgrammeShape(AMBIGUOUS, PROGRAMME_NOT_SET, ())
    return merge_programme_patterns(contributors)


def budget_form(programme_pattern: object) -> str:
    """A programme value as a scenario budget stores it: a shape id, else ``""``."""
    value = str(programme_pattern or "").strip().lower()
    return value if value in SHAPES else STANDARD


def department_budget_value(
    decision: ProgrammeShape, credit_hours: object, *, has_policy: bool = False
) -> str:
    """The ``meeting_pattern`` a scenario budget takes from its programmes.

    A shape only when the programmes agreed on it, it is a choice for the
    budget's own credit hours, and no fixed course policy (ENG101/ENG102)
    decides the course; otherwise the credit default. One function for
    Generate, Add course and the workspace, so they cannot drift apart.
    """
    if has_policy or decision.status != AGREE:
        return STANDARD
    value = budget_form(decision.meeting_pattern)
    if value == STANDARD or value not in pattern_choices(credit_hours):
        return STANDARD
    return value

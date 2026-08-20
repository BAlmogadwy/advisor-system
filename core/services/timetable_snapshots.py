"""Which snapshot of a student's term a caller is asking for, and why it matters.

WHAT USED TO MAKE THIS UNNECESSARY

``StudentTermSection`` carries two different claims in one table. A
``registration_plan_*`` row is an approved FORECAST; a ``scraper_timetable`` row is
evidence the registrar recorded a registration. Until now a term could hold only
one of them, because the two writers destroyed each other: ``apply_plan`` refused
to run against a term holding registrar rows, and a scrape deleted every row for
the term it scraped. One term, one meaning -- so a reader could take whatever it
found and be right by construction.

That guarantee is gone. Both snapshots now coexist for the same
``(student, academic_year, term)``, because a student needs to see the plan they
were given beside the registration they actually made. Every reader must now say
which of the two it means.

WHY A REQUIRED ARGUMENT RATHER THAN A DEFAULT

A default is a silent answer to a question the caller did not know it was being
asked, and the two answers differ in the worst possible direction: an expected plan
read as registration makes this system tell a student they are registered in
courses they never registered in. So :func:`select` takes an explicit
:class:`Snapshot`, ``get_student_term_baseline`` requires it as a keyword-only
argument, and a call site that has not been considered fails loudly at import or
first call rather than quietly returning the more reassuring set.

WHY THREE PROVENANCE CLASSES AND NOT TWO

``timetable_snapshot_kind`` used to split on one rule: ``registration_plan_*`` is
expected, EVERYTHING ELSE is registration evidence. That silently promoted the
staff planner's own scratch mappings -- ``planner``, ``auto_from_studying``, written
by two staff-only endpoints in ``core/planner_views.py`` -- to registrar evidence.
The student home screen then titled them "My weekly timetable" with no disclaimer:
a department's draft assignment, shown to the student as their registration. Naming
``WORKING`` as its own class is what stops that.

It does NOT mean WORKING rows are hidden. They are a forecast, so they are shown
and described as one -- see :func:`forecast_rows` and the WORKING branch of
:func:`timetable_snapshot_kind`. What the class buys is that a department's draft
can never be called a registration.

WHY ``EFFECTIVE`` RESOLVES RATHER THAN REFUSES

Several modules refuse outright when they see a mixed row set -- grep
``MIXED_REVIEW_REQUIRED``. Those refusals are correct and are deliberately kept.
They exist because a caller handed an ambiguous set has no basis for choosing, and
guessing produces "you are registered in" about a forecast. The fix is not to
delete the refusals but to stop handing those callers an ambiguous set: a caller
that asks for ``REGISTERED``, ``EXPECTED`` or ``EFFECTIVE`` receives rows of one
class only, so the refusal becomes unreachable instead of removed. If a refusal
ever fires again, that is a real contract failure and should be seen.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from enum import StrEnum
from typing import Any

#: An imported registration plan writes ``registration_plan_<year>_t<term>``. The
#: prefix, not the whole value, is the boundary: the term it was built for is part
#: of the value and is the only provenance an imported row carries.
EXPECTED_TIMETABLE_SOURCE_PREFIX = "registration_plan_"

#: Sources that are evidence the registrar recorded something.
#:
#: ``scraper_timetable`` is written by ``student_timetable_ingest`` from the portal
#: response. ``fallback_studying`` is not a database row at all -- it is synthesised
#: by ``append_unmapped_studying_courses`` from ``StudentCourse.status='studying'``,
#: which is itself scraped, for courses whose section mapping is incomplete. It is
#: registrar evidence without a section, and dropping it from ``REGISTERED`` would
#: undercount exactly the credit hours that helper exists to keep honest.
REGISTRAR_SOURCES = frozenset({"scraper_timetable", "fallback_studying"})


class SnapshotClass(StrEnum):
    """What one row's ``source`` claims about the world."""

    #: An approved forecast. NOT evidence of registration.
    EXPECTED = "expected"
    #: The registrar recorded this.
    REGISTRAR = "registrar"
    #: A staff or planner mapping. Neither approved forecast nor registration.
    WORKING = "working"


#: Which class wins when a term holds more than one and the caller asked for
#: :attr:`Snapshot.EFFECTIVE`.
#:
#: Registrar evidence first: once the registrar has recorded the term, a forecast
#: about that same term has been superseded by fact. A staff mapping outranks the
#: forecast because it is a deliberate assertion about THIS term made after the
#: plan was imported -- and because the planner writes its mapping and then
#: immediately reads the baseline back, so a policy that let the forecast win would
#: hand the planner rows it did not just write.
_EFFECTIVE_PRECEDENCE: tuple[SnapshotClass, ...] = (
    SnapshotClass.REGISTRAR,
    SnapshotClass.WORKING,
    SnapshotClass.EXPECTED,
)


class Snapshot(StrEnum):
    """What a caller means by "this student's term"."""

    #: Registrar evidence only. Use wherever the caller ASSERTS a registration:
    #: credit hours owed, academic status, exam enrolment. Returning a forecast
    #: here is how the system comes to claim a registration that does not exist.
    REGISTERED = "registered"
    #: The imported plan only, presented as a plan.
    EXPECTED = "expected"
    #: Staff and planner mappings only. Never student-facing.
    WORKING = "working"
    #: One class, chosen by :data:`_EFFECTIVE_PRECEDENCE`. Use when the question is
    #: "where does this student have to be that week" -- occupied time, clash masks,
    #: availability -- rather than "what did the registrar record".
    EFFECTIVE = "effective"
    #: Every row, provenance carried per row. Admin, maintenance and diagnostics,
    #: plus the one student-facing screen that deliberately shows both at once.
    ANY = "any"


def classify_source(source: object) -> SnapshotClass:
    """Classify one ``StudentTermSection.source`` value.

    Unknown and empty sources are ``WORKING`` rather than ``REGISTRAR``. The model
    default is ``"manual"`` and ``get_student_term_baseline`` substitutes
    ``"mapped"`` for a blank, so the permissive reading would quietly re-create the
    promotion this module exists to stop -- and a source nobody has classified is
    precisely the one nothing should be asserted from.
    """
    text = str(source or "").strip().casefold()
    if text.startswith(EXPECTED_TIMETABLE_SOURCE_PREFIX):
        return SnapshotClass.EXPECTED
    if text in REGISTRAR_SOURCES:
        return SnapshotClass.REGISTRAR
    return SnapshotClass.WORKING


def row_class(row: Mapping[str, Any]) -> SnapshotClass:
    """Classify one baseline row by its ``source`` key."""
    return classify_source(row.get("source"))


def classes_present(rows: Iterable[Mapping[str, Any]]) -> set[SnapshotClass]:
    return {row_class(row) for row in rows}


def effective_class(rows: Iterable[Mapping[str, Any]]) -> SnapshotClass | None:
    """The class :attr:`Snapshot.EFFECTIVE` would return, or ``None`` for no rows."""
    present = classes_present(rows)
    for candidate in _EFFECTIVE_PRECEDENCE:
        if candidate in present:
            return candidate
    return None


def select(
    rows: Iterable[Mapping[str, Any]],
    snapshot: Snapshot,
) -> list[Mapping[str, Any]]:
    """Return the subset of ``rows`` the caller asked for.

    Order is preserved. ``ANY`` returns every row; every other value returns rows of
    exactly ONE class, which is what makes a mixed set unreachable downstream.
    """
    materialised = list(rows)
    if snapshot is Snapshot.ANY:
        return materialised
    if snapshot is Snapshot.EFFECTIVE:
        wanted = effective_class(materialised)
        if wanted is None:
            return []
    else:
        wanted = {
            Snapshot.REGISTERED: SnapshotClass.REGISTRAR,
            Snapshot.EXPECTED: SnapshotClass.EXPECTED,
            Snapshot.WORKING: SnapshotClass.WORKING,
        }[snapshot]
    return [row for row in materialised if row_class(row) is wanted]


def forecast_rows(rows: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """The department's LATEST forecast for this term, registrar rows excluded.

    ``EXPECTED`` and ``WORKING`` are both forecasts; they differ in who authored
    them. When a term holds both, the later assertion wins — the same order
    :data:`_EFFECTIVE_PRECEDENCE` uses — because each writer now replaces only its
    own class, so an imported plan SURVIVES underneath a planner save that moved
    the student. A screen that picked ``EXPECTED`` unconditionally would show
    seating the department has already moved them out of, with no writer that ever
    removes it, while the planner and the adviser showed the newer one.

    Registrar rows are never returned: this answers "what is planned", and what is
    recorded is a different question with a different card.
    """
    materialised = list(rows)
    for candidate in _EFFECTIVE_PRECEDENCE:
        if candidate is SnapshotClass.REGISTRAR:
            continue
        chosen = [row for row in materialised if row_class(row) is candidate]
        if chosen:
            return chosen
    return []


def partition(
    rows: Iterable[Mapping[str, Any]],
) -> dict[SnapshotClass, list[Mapping[str, Any]]]:
    """Split rows by class, preserving order within each class.

    Every class is present as a key, empty when it has no rows, so a caller cannot
    read a missing key as an absent class through a ``KeyError`` it swallowed.
    """
    out: dict[SnapshotClass, list[Mapping[str, Any]]] = {
        SnapshotClass.EXPECTED: [],
        SnapshotClass.REGISTRAR: [],
        SnapshotClass.WORKING: [],
    }
    for row in rows:
        out[row_class(row)].append(row)
    return out


def timetable_snapshot_kind(rows: Iterable[Mapping[str, Any]]) -> str:
    """The label every advisor and planner payload derives its provenance from.

    Returns ``"empty"``, ``"registered"``, ``"expected"`` or ``"mixed"``. Downstream
    this becomes ``baseline_kind`` / ``schedule_kind`` — ``REGISTERED``,
    ``EXPECTED_PLAN``, ``MIXED_REVIEW_REQUIRED``, ``EMPTY`` — and those names are
    then spoken to a student, so the only thing that matters here is which side of
    "did the registrar record this" a row falls on.

    WHY WORKING ROWS ARE NOT ``"registered"``

    The rule used to be a single prefix test: ``registration_plan_*`` is expected,
    EVERYTHING ELSE is registration evidence. That put ``planner`` and
    ``auto_from_studying`` — a staff mapping and a guess at which section a student
    is in, both written by staff-only endpoints in ``core/planner_views.py`` — on
    the registrar side. Ten downstream surfaces then asserted registration from
    them: ``currently_registered_sections``, ``OUTCOME_ALREADY_REGISTERED``,
    ``registered_credits_at_planning_baseline``, and the student home heading.

    THIS VOCABULARY IS COARSER THAN THE CLASSES, DELIBERATELY

    It has one bucket for "not registrar evidence", and both ``EXPECTED`` and
    ``WORKING`` belong in it: an imported plan and a department's mapping are both
    forecasts, differing in who authored them, not in what they claim. So WORKING
    degrades to ``"expected"`` — the SAFER of the two buckets, whose presentation
    already says in every language that it is not an actual registration. Surfaces
    that need the finer distinction ask :func:`partition` for it; the student
    portal does exactly that, and shows WORKING rows under neither of its cards.
    """
    present = classes_present(rows)
    if not present:
        return "empty"
    registrar = SnapshotClass.REGISTRAR in present
    forecast = bool(present & {SnapshotClass.EXPECTED, SnapshotClass.WORKING})
    if registrar and forecast:
        return "mixed"
    return "registered" if registrar else "expected"


__all__ = [
    "EXPECTED_TIMETABLE_SOURCE_PREFIX",
    "REGISTRAR_SOURCES",
    "Snapshot",
    "SnapshotClass",
    "classes_present",
    "classify_source",
    "effective_class",
    "forecast_rows",
    "partition",
    "row_class",
    "select",
    "timetable_snapshot_kind",
]

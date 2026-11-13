"""A student telling their academic advisor what is wrong with their expected timetable.

This module is the way IN. It owns three things:

* WHAT THE STUDENT SAW. Their forecast for the planning term, grouped per section,
  frozen into a snapshot (without instructor names) and fingerprinted. The
  fingerprint is how a submission proves it is about the timetable the student
  was actually looking at.
* THE LISTS THE FORM OFFERS. Another section of the same course, from the same
  catalogue the planner offers; and the courses of the student's own plan that
  are missing from their forecast but have a section this term.
* TAKING A POINT IN. Every check, in a fixed order, then the point is written
  into the student's one open case for the term, opening it if needed.

What happens to a case afterwards (advisor transitions, replies, withdrawal) is
the workflow's. Nothing here ever writes a timetable.

WHY THE FINGERPRINT HAS NO NAMES IN IT

It is a set of 'section|LABEL|DAY|HH:MM|HH:MM|ROOM' items, plus the term and the
forecast's source. A language switch, a curriculum rename or an instructor
change is not a change to the timetable, and must never tell a student "your
timetable changed while you were writing". It is a SET because the meeting table
is unique per instructor, so a co-taught meeting appears once per instructor.

WHY TWO COURSE IDENTITIES

A section belongs to a PHYSICAL course: its code plus the name the section is
recorded under. Two sections of CS111 recorded as 'أساسيات البرمجة' and as
'برمجة (1)' are different courses, and the department groups by this identity.
The project's planner key cannot be used for it: it keeps only ASCII, so every
Arabic name collapses to the bare code. The student's PLAN names the course its
own way, through the programme requirement, and that is kept beside it.

WHAT A POINT IS

An anchor (a section, the whole timetable, or no timetable), a reason, what was
asked for instead (nothing, another section, or a missing course), and the
timetable it was raised about. The same four again is the same point; any one
of them different is another point.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import F, Q, QuerySet
from django.utils import timezone

from core.models import (
    TIMETABLE_FEEDBACK_TERMINAL_STATUSES,
    ProgrammeRequirement,
    Student,
    TermSection,
    TimetableFeedback,
    TimetableFeedbackEvent,
    TimetableFeedbackPoint,
    TimetableFeedbackReason,
)
from core.services import rate_limit
from core.services.course_identity import planner_course_key
from core.services.eligibility import evaluate_prerequisites
from core.services.planner_drafts import planning_term
from core.services.section_programmes import filter_sections_for_program
from core.services.student_helpers import (
    get_prerequisites,
    get_student_passed_and_studying,
    is_elective_slot,
    normalize_code,
)
from core.services.student_sections import (
    UnknownStudentGender,
    arabic_term_section_course_names,
    cohort_of_student_section,
    gender_section_filter,
    normalize_section_label,
    section_course_key,
    section_is_available_to_student,
    student_gender_strict,
)
from core.services.student_term_timetable import (
    StudentTermTimetable,
    TimetableUnavailable,
    read_student_term_timetable,
)
from core.services.timetable_feedback_workflow import (
    answer_window_passed,
    close_settled_answer,
    reopen_target,
    storable,
    visible,
)

SNAPSHOT_SCHEMA = 1
FINGERPRINT_SCHEMA = 1
#: A note is for context, not an essay; a longer one belongs in a meeting.
NOTE_MAX_CHARS = 1000
#: Points a student may have live in one case at once.
MAX_LIVE_POINTS = 8
#: Cases a student may open in one term, counting every status.
MAX_CASES_PER_TERM = 3
#: How long after withdrawing a case a new one may be opened.
COOL_DOWN_AFTER_WITHDRAWAL = timedelta(hours=24)

R = TimetableFeedbackReason
S = TimetableFeedback.Status

#: Which reasons each anchor can carry. A reason that fits no anchor is refused
#: before anything is written.
REASONS_BY_ANCHOR: dict[str, frozenset[str]] = {
    "SECTION": frozenset(
        {
            R.TIME_CLASH,
            R.TIME_UNSUITABLE,
            R.PREFER_OTHER_SECTION,
            R.ALREADY_PASSED,
            R.PREREQUISITE_NOT_MET,
            R.NOT_IN_MY_PLAN,
            R.SECTION_HAS_NO_TIMES,
            R.OTHER,
        }
    ),
    "TIMETABLE": frozenset(
        {
            R.LOAD_TOO_HIGH,
            R.LOAD_TOO_LOW,
            R.MISSING_COURSE,
            R.TOO_MANY_CAMPUS_DAYS,
            R.GRADUATION_CONCERN,
            R.OTHER,
        }
    ),
    "NO_FORECAST": frozenset({R.NO_FORECAST}),
}
#: The reasons that may name another section of the same course instead.
SECTION_REQUEST_REASONS = frozenset({R.TIME_CLASH, R.TIME_UNSUITABLE, R.PREFER_OTHER_SECTION})

_DAY_CODES = frozenset({"SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"})
_DAY_NAMES = {
    "SUNDAY": "SUN",
    "MONDAY": "MON",
    "TUESDAY": "TUE",
    "WEDNESDAY": "WED",
    "THURSDAY": "THU",
    "FRIDAY": "FRI",
    "SATURDAY": "SAT",
    "الأحد": "SUN",
    "الاحد": "SUN",
    "الإثنين": "MON",
    "الاثنين": "MON",
    "الثلاثاء": "TUE",
    "الأربعاء": "WED",
    "الاربعاء": "WED",
    "الخميس": "THU",
    "الجمعة": "FRI",
    "السبت": "SAT",
}


# ---------------------------------------------------------------------------
# What the student saw
# ---------------------------------------------------------------------------


def normalise_day(value: object) -> str:
    """'SUN', 'Sunday', ' sun ' and 'الأحد' are one day: SUN."""
    text = " ".join(str(value or "").split())
    key = text.upper()
    if key in _DAY_CODES:
        return key
    return _DAY_NAMES.get(key) or _DAY_NAMES.get(text) or key


def normalise_time(value: object) -> str:
    """'9:00', '09:00' and '09:00:00' are one time: 09:00. Anything else as given."""
    text = str(value or "").strip()
    hours, _, rest = text.partition(":")
    minutes = rest[:2]
    if hours.isdecimal() and len(minutes) == 2 and minutes.isdecimal():
        return f"{int(hours):02d}:{minutes}"
    return text


def normalise_room(value: object) -> str:
    return " ".join(str(value or "").split()).upper()


def _normalise_name(value: object) -> str:
    """A course name as one comparable spelling, keeping Arabic letters."""
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).split()).upper()


def section_course_identity(course_key: object, course_name: object) -> str:
    """The PHYSICAL course of a section: CODE::<its recorded name>, or the bare code."""
    code = normalize_code(course_key)
    name = _normalise_name(course_name)
    return f"{code}::{name}" if name and name != code else code


Meeting = tuple[str, str, str, str]


@dataclass(frozen=True)
class SeenSection:
    """One section of the forecast as the student saw it, minus instructor names."""

    term_section_id: int
    course_key: str
    section: str
    stored_course_name: str
    displayed_course_name: str
    section_course_identity: str
    plan_course_identity: str
    #: False when the course is off the student's plan, or its bare code names
    #: more than one course among the recorded sections.
    identity_certain: bool
    credits: int
    #: False for an off-plan course, whose credits the reader reports as 0.
    credits_known: bool
    #: (DAY, HH:MM, HH:MM, ROOM), normalised, each once, sorted.
    meetings: tuple[Meeting, ...]

    def as_snapshot(self) -> dict[str, Any]:
        return {
            "term_section_id": self.term_section_id,
            "course_key": self.course_key,
            "section_course_identity": self.section_course_identity,
            "plan_course_identity": self.plan_course_identity,
            "identity_certain": self.identity_certain,
            "stored_course_name": self.stored_course_name,
            "displayed_course_name": self.displayed_course_name,
            "section": self.section,
            "credits": self.credits,
            "credits_known": self.credits_known,
            "meetings": [list(meeting) for meeting in self.meetings],
        }


def _meetings_of(rows: Iterable[Mapping[str, Any]]) -> tuple[Meeting, ...]:
    meetings = {
        (
            normalise_day(row.get("day")),
            normalise_time(row.get("start_time")),
            normalise_time(row.get("end_time")),
            normalise_room(row.get("room")),
        )
        for row in rows
        if str(row.get("day") or "").strip() and str(row.get("start_time") or "").strip()
    }
    return tuple(sorted(meetings))


def _requirements(programme: str) -> dict[str, ProgrammeRequirement]:
    """The student's programme requirements by normalised course code."""
    out: dict[str, ProgrammeRequirement] = {}
    if not programme:
        return out
    for requirement in ProgrammeRequirement.objects.filter(program__iexact=programme):
        code = normalize_code(requirement.course_code)
        if code and code not in out:
            out[code] = requirement
    return out


def _recorded_names(codes: Iterable[str]) -> dict[str, set[str]]:
    """Every distinct name each bare code is recorded under, across GLOBAL sections."""
    wanted = {normalize_code(code) for code in codes if normalize_code(code)}
    if not wanted:
        return {}
    predicate = Q()
    for code in sorted(wanted):
        predicate |= Q(course_key__iexact=code)
    names: dict[str, set[str]] = defaultdict(set)
    for raw_key, raw_name in TermSection.objects.filter(
        predicate, scenario__isnull=True
    ).values_list("course_key", "course_name"):
        code = normalize_code(raw_key)
        name = _normalise_name(raw_name)
        if code in wanted and name:
            names[code].add(name)
    return names


def seen_sections(
    timetable: StudentTermTimetable, *, programme: str, language: str = "en"
) -> list[SeenSection]:
    """The forecast's sections as the student saw them, ordered by section id."""
    by_section: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in timetable.forecast_rows:
        section_id = row.get("term_section_id")
        if isinstance(section_id, int):
            by_section[section_id].append(row)
    if not by_section:
        return []

    codes = {normalize_code(rows[0].get("course_code")) for rows in by_section.values()}
    requirements = _requirements(programme)
    recorded = _recorded_names(codes)
    arabic = arabic_term_section_course_names(sorted(codes)) if language == "ar" else {}

    out = []
    for section_id in sorted(by_section):
        rows = by_section[section_id]
        first = rows[0]
        code = normalize_code(first.get("course_code"))
        stored = str(first.get("course_name") or "")
        requirement = requirements.get(code)
        on_plan = requirement is not None
        out.append(
            SeenSection(
                term_section_id=section_id,
                course_key=code,
                section=normalize_section_label(first.get("section")),
                stored_course_name=stored,
                displayed_course_name=arabic.get(code) or stored,
                section_course_identity=section_course_identity(code, stored),
                plan_course_identity=(
                    planner_course_key(code, requirement.course_name) if requirement else code
                ),
                identity_certain=on_plan and len(recorded.get(code, set())) <= 1,
                credits=int(first.get("credits") or 0),
                credits_known=on_plan,
                meetings=_meetings_of(rows),
            )
        )
    return out


def forecast_fingerprint(
    *, academic_year: object, term: object, forecast_class: str, sections: Iterable[SeenSection]
) -> str:
    """The identity of a forecast: which sections, meeting when and where. No names."""
    items: set[str] = set()
    for section in sections:
        if not section.meetings:
            items.add(f"{section.term_section_id}|{section.section}|-")
        for day, start, end, room in section.meetings:
            items.add(f"{section.term_section_id}|{section.section}|{day}|{start}|{end}|{room}")
    material = {
        "schema": FINGERPRINT_SCHEMA,
        "term": f"{academic_year}/{term}",
        "forecast_class": forecast_class,
        "items": sorted(items),
    }
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _minutes(value: str) -> int:
    hours, _, minutes = value.partition(":")
    if hours.isdecimal() and minutes.isdecimal():
        return int(hours) * 60 + int(minutes)
    return -1


def _overlap(a: Meeting, b: Meeting) -> bool:
    """Same day and overlapping in time. Touching end to start is not a clash."""
    if a[0] != b[0]:
        return False
    a_start, a_end, b_start, b_end = _minutes(a[1]), _minutes(a[2]), _minutes(b[1]), _minutes(b[2])
    if a_start < 0 or a_end <= a_start or b_start < 0 or b_end <= b_start:
        return False
    return a_start < b_end and b_start < a_end


def clashes_between(sections: Iterable[SeenSection]) -> list[dict[str, Any]]:
    """Every pair of sections that meets at the same time, by section id and day."""
    ordered = sorted(sections, key=lambda section: section.term_section_id)
    found: set[tuple[int, int, str]] = set()
    for index, a in enumerate(ordered):
        for b in ordered[index + 1 :]:
            for ma in a.meetings:
                for mb in b.meetings:
                    if _overlap(ma, mb):
                        found.add((a.term_section_id, b.term_section_id, ma[0]))
    return [
        {"a_term_section_id": a, "b_term_section_id": b, "day": day} for a, b, day in sorted(found)
    ]


SNAPSHOT_KEYS = frozenset(
    {
        "schema",
        "academic_year",
        "term",
        "forecast_class",
        "submitted_language",
        "sections",
        "has_registration",
        "forecast_not_registered",
        "registered_not_forecast",
        "programme",
        "cohort",
        "standing",
        "facts",
    }
)
SNAPSHOT_SECTION_KEYS = frozenset(
    {
        "term_section_id",
        "course_key",
        "section_course_identity",
        "plan_course_identity",
        "identity_certain",
        "stored_course_name",
        "displayed_course_name",
        "section",
        "credits",
        "credits_known",
        "meetings",
    }
)
SNAPSHOT_FACT_KEYS = frozenset(
    {
        "clashes",
        "timetabled_credits",
        "unknown_credit_sections",
        "requested_clashes",
        "requested_prerequisites_missing",
    }
)
SNAPSHOT_CLASH_KEYS = frozenset({"a_term_section_id", "b_term_section_id", "day"})


def validate_point_snapshot(snapshot: dict[str, Any]) -> None:
    """Refuse any key outside the allowlist, at every level, and any odd shape.

    The snapshot is what the advisor and the department will read for years. A
    key that slipped in (an instructor's name, a seat count, another student)
    would be frozen there with it, so the shape is checked, not trusted: the
    top level, each section, each meeting (exactly day, start, end, room), the
    facts, each clash, and the two requested lists.
    """
    if not isinstance(snapshot, dict) or set(snapshot) != SNAPSHOT_KEYS:
        raise ValueError(f"snapshot keys must be exactly {sorted(SNAPSHOT_KEYS)}")
    if snapshot["schema"] != SNAPSHOT_SCHEMA:
        raise ValueError(f"unknown snapshot schema {snapshot['schema']!r}")
    for section in snapshot["sections"]:
        if not isinstance(section, dict) or set(section) != SNAPSHOT_SECTION_KEYS:
            raise ValueError(
                f"snapshot section keys must be exactly {sorted(SNAPSHOT_SECTION_KEYS)}"
            )
        for meeting in section["meetings"]:
            if not isinstance(meeting, list) or len(meeting) != 4:
                raise ValueError("a meeting is exactly [day, start, end, room]")
    facts = snapshot["facts"]
    if not isinstance(facts, dict) or set(facts) != SNAPSHOT_FACT_KEYS:
        raise ValueError(f"snapshot fact keys must be exactly {sorted(SNAPSHOT_FACT_KEYS)}")
    for clash in facts["clashes"]:
        if not isinstance(clash, dict) or set(clash) != SNAPSHOT_CLASH_KEYS:
            raise ValueError(f"a clash is exactly {sorted(SNAPSHOT_CLASH_KEYS)}")
    if not all(isinstance(value, int) for value in facts["requested_clashes"]):
        raise ValueError("requested clashes are section ids")
    if not all(isinstance(value, str) for value in facts["requested_prerequisites_missing"]):
        raise ValueError("missing prerequisites are course codes")


# ---------------------------------------------------------------------------
# The lists the form offers
# ---------------------------------------------------------------------------


def catalogue_sections_for(student_id: int, programme: str) -> list[TermSection]:
    """Global sections the student could take: their programme's, their cohort's,
    and only those with a complete meeting time. The planner's own catalogue rule.

    A student whose cohort is not recorded is offered nothing: guessing a cohort
    would offer sections of the other one.
    """
    try:
        gender = student_gender_strict(student_id)
    except UnknownStudentGender:
        return []
    sections = (
        filter_sections_for_program(TermSection.objects.filter(scenario__isnull=True), programme)
        .filter(gender_section_filter(gender))
        .prefetch_related("meetings")
        .order_by("course_key", "section")
    )
    return [
        section
        for section in sections
        if any(m.day and m.start_time and m.end_time for m in section.meetings.all())
    ]


def alternative_sections_for(
    student_id: int, programme: str, anchor: SeenSection
) -> list[TermSection]:
    """The other sections of the anchor's course the student could take instead.

    From the same catalogue as everything else the form offers. The same course
    is the same CODE::NAME, never just the code: two courses can share a code,
    and a section of the other one is not an alternative. The form offers exactly
    these, and the submission accepts exactly these.
    """
    return [
        section
        for section in catalogue_sections_for(student_id, programme)
        if section.pk != anchor.term_section_id
        and section_course_identity(section_course_key(section), section.course_name)
        == anchor.section_course_identity
    ]


def missing_course_candidates(
    student_id: int,
    programme: str,
    timetable: StudentTermTimetable,
    catalogue: Iterable[TermSection],
) -> list[dict[str, str]]:
    """The courses a student may say are missing: in their own plan, not passed,
    not already in the forecast, not an elective placeholder, and with at least
    one section in the catalogue this term. A project with no section, or a course
    nobody is teaching, is never offered."""
    passed, _studying = get_student_passed_and_studying(student_id)
    in_forecast = timetable.forecast_codes
    offered = {section_course_key(section) for section in catalogue}
    out = []
    for code, requirement in sorted(_requirements(programme).items()):
        if (
            code in passed
            or code in in_forecast
            or code not in offered
            or is_elective_slot(requirement.type)
        ):
            continue
        out.append(
            {
                "course_key": code,
                "plan_course_identity": planner_course_key(code, requirement.course_name),
                "course_name": str(requirement.course_name or ""),
            }
        )
    return out


# ---------------------------------------------------------------------------
# Taking a point in
# ---------------------------------------------------------------------------


class FeedbackRefused(Exception):
    """A submission that was not taken in, and why. ``status`` is the HTTP answer.

    ``NOT_OFFERED`` is deliberately one code for every problem with a requested
    section or course, so the form cannot be used to probe which ids exist.
    """

    def __init__(
        self,
        code: str,
        *,
        status: int = 400,
        retry_after: int = 0,
        fingerprint: str = "",
        reference: str = "",
    ) -> None:
        super().__init__(code)
        self.code = code
        self.status = status
        self.retry_after = retry_after
        #: For TIMETABLE_CHANGED: the forecast the form must be redrawn with.
        self.fingerprint = fingerprint
        #: For ALREADY_RAISED: the case the earlier point is in.
        self.reference = reference


@dataclass(frozen=True)
class Submitted:
    reference: str
    point_id: Any
    created_case: bool
    #: The exact point was already there; nothing new was written.
    duplicate: bool = False
    #: The case had been answered; this point reopened it.
    reopened: bool = False


def accepting_for(programme: object) -> bool:
    """Whether new cases and points are being taken from this programme."""
    if not (settings.TIMETABLE_FEEDBACK_ENABLED and settings.TIMETABLE_FEEDBACK_ACCEPTING):
        return False
    pilot = settings.TIMETABLE_FEEDBACK_PROGRAMMES
    return not pilot or str(programme or "").strip().upper() in pilot


def _spend(budget: str, student_id: int, now: datetime) -> None:
    decision = rate_limit.consume(budget, student_id, now=now)
    if not decision.allowed:
        raise FeedbackRefused("RATE_LIMITED", status=429, retry_after=decision.retry_after)


def _language(value: object) -> str:
    return "ar" if str(value or "").strip().lower().startswith("ar") else "en"


def _parse_anchor(anchor: object, sections: dict[int, SeenSection]) -> tuple[str, int | None]:
    text = str(anchor or "").strip()
    if text in ("TIMETABLE", "NO_FORECAST"):
        return text, None
    kind, _, raw_id = text.partition("|")
    if kind == "SECTION" and raw_id.isdecimal():
        section = sections.get(int(raw_id))
        # A section with no label cannot be named in a point (a database check),
        # so it is no anchor; refusing here keeps that a 400, never a 500.
        if section is not None and section.section:
            return "SECTION", section.term_section_id
    raise FeedbackRefused("UNKNOWN_ANCHOR")


def _requested_section(
    *,
    student_id: int,
    programme: str,
    anchor: SeenSection,
    requested_id: object,
) -> TermSection:
    """One of the alternatives the form offered for this anchor, or NOT_OFFERED."""
    text = str(requested_id).strip()
    if not text.isdecimal():
        raise FeedbackRefused("NOT_OFFERED")
    wanted = int(text)
    for section in alternative_sections_for(student_id, programme, anchor):
        if section.pk != wanted:
            continue
        try:
            # Catalogue membership already implies this; the canonical check is
            # asked anyway, so a later change to either cannot widen the other.
            available = section_is_available_to_student(section, student_id=student_id)
        except UnknownStudentGender:
            available = False
        if available:
            return section
        break
    raise FeedbackRefused("NOT_OFFERED")


def _missing_prerequisites(
    student_id: int, programme: str, code: str, profile: Mapping[str, Any]
) -> list[str]:
    passed, studying = get_student_passed_and_studying(student_id)
    outcome = evaluate_prerequisites(
        get_prerequisites(code, programme),
        passed,
        studying,
        earned_credits=int(profile.get("total_earned_credits") or 0),
        registered_credits=int(profile.get("current_registered_credits") or 0),
    )
    return sorted(outcome.missing)


def _refuse_a_new_case_if_capped(
    student_id: int, academic_year: str, term: str, now: datetime
) -> None:
    """Three cases a term, counting every status, and a day after a withdrawal."""
    this_term = TimetableFeedback.objects.filter(
        student_id=student_id, academic_year=academic_year, term=term
    )
    if this_term.count() >= MAX_CASES_PER_TERM:
        raise FeedbackRefused("TOO_MANY_CASES", status=409)
    cutoff = now - COOL_DOWN_AFTER_WITHDRAWAL
    if this_term.filter(
        Q(closed_at__gt=cutoff) | Q(closed_at__isnull=True, updated_at__gt=cutoff),
        status=S.WITHDRAWN,
    ).exists():
        raise FeedbackRefused("COOLING_DOWN", status=409)


def _open_cases(student_id: int, academic_year: str, term: str) -> QuerySet[TimetableFeedback]:
    return TimetableFeedback.objects.filter(
        student_id=student_id, academic_year=academic_year, term=term
    ).exclude(status__in=TIMETABLE_FEEDBACK_TERMINAL_STATUSES)


def _live_point(
    case: TimetableFeedback, *, anchor_key: str, reason: str, request_key: str, fingerprint: str
) -> TimetableFeedbackPoint | None:
    """The live point a new one would repeat: same anchor, reason, request and timetable."""
    return case.points.filter(
        anchor_key=anchor_key,
        reason_code=reason,
        request_key=request_key,
        seen_fingerprint=fingerprint,
        withdrawn_at__isnull=True,
    ).first()


def _open_case(
    *,
    student_id: int,
    academic_year: str,
    term: str,
    profile: Mapping[str, Any],
    cohort: str,
    timetable: StudentTermTimetable,
    language: str,
    now: datetime,
) -> tuple[TimetableFeedback, bool]:
    """The student's one open case for the term, under its row lock, opened if there
    is none. The caps are checked again here, under the lock, because the check
    before the budgets were spent could have been overtaken.

    An answered case whose window has passed is settled, not open to this point:
    it is closed here, and the point opens a new case under the same caps. A
    refusal undoes the closing with everything else."""
    open_cases = _open_cases(student_id, academic_year, term).select_for_update()
    # Read twice when the first read finds nothing. If it waited on a case that
    # was closed meanwhile (a settled answer giving way to a new case), the case
    # opened in its place is newer than that read's snapshot, and only a fresh
    # statement sees it. Without the second read this point would count that new
    # case towards the cap instead of joining it.
    case = open_cases.first() or open_cases.first()
    if case is not None and case.status == S.ANSWERED and answer_window_passed(case, now):
        close_settled_answer(case, now=now)
        case = None
    if case is not None:
        return case, False

    _refuse_a_new_case_if_capped(student_id, academic_year, term, now)
    try:
        with transaction.atomic():
            case = TimetableFeedback.objects.create(
                student_id=student_id,
                academic_year=academic_year,
                term=term,
                routed_advisor_id=str(profile.get("advisor_id") or ""),
                program_at_submission=str(profile.get("program") or ""),
                cohort_at_submission=cohort,
                standing_at_submission=str(profile.get("status") or ""),
                has_registration_at_submission=timetable.has_registration,
                submitted_language=language,
                last_student_activity_at=now,
            )
    except IntegrityError:
        # Another request opened this term's case first. It is the student's own,
        # so the point goes into it rather than failing.
        case = open_cases.first()
        if case is None:
            raise
        return case, False
    TimetableFeedbackEvent.objects.create(case=case, kind=TimetableFeedbackEvent.Kind.SUBMITTED)
    return case, True


def submit_point(
    *,
    student_id: int,
    anchor: str,
    reason: str,
    seen_fingerprint: str,
    note: str = "",
    requested_term_section_id: object = None,
    requested_course_key: str = "",
    language: str = "en",
    now: datetime | None = None,
) -> Submitted:
    """Take one point into the student's open case for the planning term.

    The checks run in this order, each refusing with a ``FeedbackRefused``:

    0. Feedback is being taken from the student's programme; the attempt budget
       is spent before the forecast is rebuilt.
    a. The forecast is rebuilt; a failed read is TIMETABLE_UNAVAILABLE, never an
       empty timetable.
    b. The form's fingerprint is the rebuilt one, so the frozen copy really is
       what the student was looking at. First, so that a section which left the
       forecast while they wrote reads as "your timetable changed".
    c. The anchor is a labelled section of that forecast, the timetable as a whole
       (only when there is one), or "no timetable".
    d. The reason fits the anchor; a note is given for OTHER (one of only
       invisible marks counts as none), is not too long, and can be stored: no
       control character beyond a tab or a line break, and no lone surrogate.
    e. A requested section or course is one the form offered.
    f. "No timetable" only when there is neither a forecast nor a registration.
    g. Opening a case keeps to the per-term cap and the cool-down after a
       withdrawal: checked here, before any success budget is spent, and again
       under the case lock.
    h. The success budgets are spent, duplicates included.

    Then, under the case's row lock: an answered case past its reopening window is
    closed and a new case opened instead; the exact point already there returns
    its reference; the same point with a different note is ALREADY_RAISED; a ninth
    live point is refused. The case's version (and, if it had been ANSWERED, its
    reopening) is claimed and the point written together, in one savepoint.
    """
    now = now or timezone.now()
    language = _language(language)
    note = str(note or "").strip()
    if not visible(note):
        # Only zero-width or direction marks: no note at all.
        note = ""
    profile = (
        Student.objects.filter(student_id=student_id)
        .values(
            "program",
            "section",
            "status",
            "advisor_id",
            "total_earned_credits",
            "current_registered_credits",
        )
        .first()
    )
    if profile is None:
        raise FeedbackRefused("UNKNOWN_STUDENT", status=404)
    programme = str(profile.get("program") or "")
    cohort = cohort_of_student_section(profile.get("section"))

    # 0.
    if not accepting_for(programme):
        raise FeedbackRefused("NOT_ACCEPTING", status=403)
    _spend(rate_limit.TIMETABLE_FEEDBACK_ATTEMPT, student_id, now)

    # a.
    academic_year, term = planning_term()
    try:
        timetable = read_student_term_timetable(student_id, academic_year, term)
    except TimetableUnavailable as exc:
        raise FeedbackRefused("TIMETABLE_UNAVAILABLE", status=503) from exc
    sections = seen_sections(timetable, programme=programme, language=language)
    by_id = {section.term_section_id: section for section in sections}
    fingerprint = forecast_fingerprint(
        academic_year=academic_year,
        term=term,
        forecast_class=timetable.forecast_class,
        sections=sections,
    )

    # b.
    if str(seen_fingerprint or "") != fingerprint:
        raise FeedbackRefused("TIMETABLE_CHANGED", status=409, fingerprint=fingerprint)

    # c.
    anchor_kind, anchor_id = _parse_anchor(anchor, by_id)
    if anchor_kind == "TIMETABLE" and not sections:
        raise FeedbackRefused("UNKNOWN_ANCHOR")

    # d.
    anchor_section = by_id.get(anchor_id) if anchor_id is not None else None
    if reason not in REASONS_BY_ANCHOR[anchor_kind]:
        raise FeedbackRefused("REASON_DOES_NOT_FIT")
    if reason == R.SECTION_HAS_NO_TIMES and anchor_section and anchor_section.meetings:
        raise FeedbackRefused("REASON_DOES_NOT_FIT")
    if reason == R.OTHER and not note:
        raise FeedbackRefused("NOTE_REQUIRED")
    if len(note) > NOTE_MAX_CHARS:
        raise FeedbackRefused("NOTE_TOO_LONG")
    # Line breaks and tabs are writing; any other control character, or a lone
    # surrogate, is not, and would otherwise be a server error at the insert.
    if not storable(note):
        raise FeedbackRefused("NOTE_INVALID")

    # e.
    requested_section = None
    requested_code = ""
    requested_plan_identity = ""
    wants_section = requested_term_section_id not in (None, "")
    wants_course = bool(str(requested_course_key or "").strip())
    if reason == R.PREFER_OTHER_SECTION and not wants_section:
        raise FeedbackRefused("NOT_OFFERED")
    if reason == R.MISSING_COURSE and not wants_course:
        raise FeedbackRefused("NOT_OFFERED")
    if wants_section:
        if reason not in SECTION_REQUEST_REASONS or anchor_section is None:
            raise FeedbackRefused("NOT_OFFERED")
        requested_section = _requested_section(
            student_id=student_id,
            programme=programme,
            anchor=anchor_section,
            requested_id=requested_term_section_id,
        )
    if wants_course:
        if reason != R.MISSING_COURSE:
            raise FeedbackRefused("NOT_OFFERED")
        requested_code = normalize_code(requested_course_key)
        candidates = missing_course_candidates(
            student_id, programme, timetable, catalogue_sections_for(student_id, programme)
        )
        candidate = next((c for c in candidates if c["course_key"] == requested_code), None)
        if candidate is None:
            raise FeedbackRefused("NOT_OFFERED")
        requested_plan_identity = candidate["plan_course_identity"]

    # f.
    if anchor_kind == "NO_FORECAST" and (sections or timetable.has_registration):
        raise FeedbackRefused("NO_FORECAST_NOT_ALLOWED")

    # The frozen evidence: computed for the record, never a reason to refuse.
    requested_clashes: list[int] = []
    if requested_section is not None:
        requested_meetings = _meetings_of(
            {
                "day": m.day,
                "start_time": m.start_time,
                "end_time": m.end_time,
                "room": m.room,
            }
            for m in requested_section.meetings.all()
        )
        requested_clashes = sorted(
            section.term_section_id
            for section in sections
            if section.term_section_id != anchor_id
            and any(_overlap(a, b) for a in requested_meetings for b in section.meetings)
        )
    snapshot = {
        "schema": SNAPSHOT_SCHEMA,
        "academic_year": academic_year,
        "term": term,
        "forecast_class": timetable.forecast_class,
        "submitted_language": language,
        "sections": [section.as_snapshot() for section in sections],
        "has_registration": timetable.has_registration,
        "forecast_not_registered": timetable.forecast_not_registered,
        "registered_not_forecast": timetable.registered_not_forecast,
        "programme": programme,
        "cohort": cohort,
        "standing": str(profile.get("status") or ""),
        "facts": {
            "clashes": clashes_between(sections),
            "timetabled_credits": sum(s.credits for s in sections if s.credits_known),
            "unknown_credit_sections": sum(1 for s in sections if not s.credits_known),
            "requested_clashes": requested_clashes,
            "requested_prerequisites_missing": (
                _missing_prerequisites(student_id, programme, requested_code, profile)
                if requested_code
                else []
            ),
        },
    }
    validate_point_snapshot(snapshot)
    anchor_key = f"SECTION|{anchor_id}" if anchor_kind == "SECTION" else anchor_kind
    if requested_section is not None:
        request_key = f"SECTION|{requested_section.pk}"
    elif requested_code:
        request_key = f"COURSE|{requested_code}"
    else:
        request_key = ""

    # g. Before anything is spent; checked again under the lock. A settled answer
    # is about to give way to a new case, so it counts as no open case here.
    open_now = _open_cases(student_id, academic_year, term).first()
    if open_now is None or (open_now.status == S.ANSWERED and answer_window_passed(open_now, now)):
        _refuse_a_new_case_if_capped(student_id, academic_year, term, now)

    # h.
    _spend(rate_limit.TIMETABLE_FEEDBACK, student_id, now)
    _spend(rate_limit.TIMETABLE_FEEDBACK_DAILY, student_id, now)

    point_key = {
        "anchor_key": anchor_key,
        "reason": reason,
        "request_key": request_key,
        "fingerprint": fingerprint,
    }
    with transaction.atomic():
        case, created = _open_case(
            student_id=student_id,
            academic_year=academic_year,
            term=term,
            profile=profile,
            cohort=cohort,
            timetable=timetable,
            language=language,
            now=now,
        )
        same_point = _live_point(case, **point_key)
        if same_point is not None:
            if same_point.note == note:
                return Submitted(
                    reference=case.reference,
                    point_id=same_point.pk,
                    created_case=created,
                    duplicate=True,
                )
            raise FeedbackRefused("ALREADY_RAISED", status=409, reference=case.reference)
        if case.points.filter(withdrawn_at__isnull=True).count() >= MAX_LIVE_POINTS:
            raise FeedbackRefused("TOO_MANY_POINTS", status=409)

        # An answered case still open here is inside its window, so it is the
        # student's to reopen: a new point puts it back in front of whoever
        # handles it, or back in the queue if nobody does.
        reopened_to = reopen_target(case) if case.status == S.ANSWERED else ""
        changes: dict[str, Any] = {
            "version": F("version") + 1,
            "last_student_activity_at": now,
            "updated_at": now,
        }
        if reopened_to:
            changes["status"] = reopened_to
        try:
            # The claim and the write succeed or fail together.
            with transaction.atomic():
                claimed = (
                    TimetableFeedback.objects.filter(
                        pk=case.pk, version=case.version, status=case.status
                    )
                    .exclude(status__in=TIMETABLE_FEEDBACK_TERMINAL_STATUSES)
                    .update(**changes)
                )
                if claimed != 1:
                    raise FeedbackRefused("CASE_CHANGED", status=409)
                point = TimetableFeedbackPoint.objects.create(
                    case=case,
                    anchor_kind=anchor_kind,
                    term_section_id=anchor_id,
                    course_key=anchor_section.course_key if anchor_section else "",
                    section_label=anchor_section.section if anchor_section else "",
                    section_course_identity=(
                        anchor_section.section_course_identity if anchor_section else ""
                    ),
                    plan_course_identity=(
                        anchor_section.plan_course_identity if anchor_section else ""
                    ),
                    identity_certain=bool(anchor_section and anchor_section.identity_certain),
                    anchor_key=anchor_key,
                    reason_code=reason,
                    requested_term_section_id=requested_section.pk if requested_section else None,
                    requested_section_label=(
                        normalize_section_label(requested_section.section)
                        if requested_section
                        else ""
                    ),
                    requested_course_key=requested_code,
                    requested_plan_course_identity=requested_plan_identity,
                    request_key=request_key,
                    note=note,
                    seen_snapshot=snapshot,
                    seen_fingerprint=fingerprint,
                )
        except IntegrityError:
            # The same point landed between the check above and this write. The
            # claim was undone with the write; the earlier point answers for it.
            existing = _live_point(case, **point_key)
            if existing is None:
                raise
            if existing.note != note:
                raise FeedbackRefused(
                    "ALREADY_RAISED", status=409, reference=case.reference
                ) from None
            return Submitted(
                reference=case.reference, point_id=existing.pk, created_case=created, duplicate=True
            )
        if reopened_to:
            TimetableFeedbackEvent.objects.create(
                case=case,
                kind=TimetableFeedbackEvent.Kind.REOPENED,
                from_value=S.ANSWERED,
                to_value=reopened_to,
                point_id=point.pk,
            )
        TimetableFeedbackEvent.objects.create(
            case=case, kind=TimetableFeedbackEvent.Kind.POINT_ADDED, point_id=point.pk
        )
    return Submitted(
        reference=case.reference,
        point_id=point.pk,
        created_case=created,
        reopened=bool(reopened_to),
    )

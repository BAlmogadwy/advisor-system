"""What students ask of their timetables: the timetable feedback report.

The department's board, and each advisor's own. Every number is drawn from the
cases a member of staff may open (`timetable_feedback_staff.visible_to`), never
from more: an advisor's report is about their own students and the cases the
department assigned them, the department's about its programmes, and an
administrator's about everyone.

WHAT IS COUNTED. Students, where the question is how many asked: one student who
raised the same section three times is one student. Live points, where the
question is what was asked: a withdrawn point says nothing any more, and nor
does any point of a case the student withdrew. Cases, for the work: how many
wait on whom, and how soon staff first acted; a withdrawn case still counts
there, by status and by day.

THE QUEUE AGREES. Each narrowing is a condition on cases defined here once
(`term_q`, `sent_since_q`, `sent_from_programme_q`), and the queue applies the
same conditions to the links the report gives it, so a row's count and the cases
the queue then lists are the same cases.

WHAT A ROW IS ABOUT. A section is the PHYSICAL section the student was shown (its
course as CODE::NAME and its label), which is what the department can change. A
missing course is the course as the student's own plan names it. Both are the
frozen values on the point, so a re-import never moves a count.

THE PROGRAMME is the one the student was in when they wrote
(`program_at_submission`): the report is about the timetable they were shown,
and a student who moves programme does not take their feedback along.
"""

from __future__ import annotations

import re
import statistics
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from django.db.models import Count, Exists, OuterRef, Q, QuerySet
from django.db.models.functions import Trim, TruncDate, Upper
from django.utils import timezone

from core.models import (
    TIMETABLE_FEEDBACK_TERMINAL_STATUSES,
    ProgrammeRequirement,
    Student,
    TimetableFeedback,
    TimetableFeedbackPoint,
)
from core.services import timetable_feedback as feedback
from core.services import timetable_feedback_staff as staff_service
from core.services.course_identity import planner_course_key
from core.services.credit_policy import EXPECTED_GRADUATE_STATUS

S = TimetableFeedback.Status
R = TimetableFeedbackPoint.Reason

#: Rows in each ranked table; the rest are counted, not listed.
SECTION_ROWS = 12
MOVE_ROWS = 10
MISSING_ROWS = 10
#: The periods the report can be narrowed to, in days; '' is the whole term.
WINDOWS = ("7", "30", "90")
#: Days in the timeline when no period is chosen.
TIMELINE_DAYS = 30
#: How soon a first staff action counts as prompt.
PROMPT = timedelta(days=2)
#: What the term filter says for every term at once.
EVERY_TERM = "all"


def term_key(academic_year: object, term: object) -> str:
    return f"{academic_year}/{term}"


def programme_code() -> Upper:
    """The programme the student wrote in, as the code the filters offer: an
    import can leave spaces around it, and the lists strip them too."""
    return Upper(Trim("program_at_submission"))


# The report's narrowings as conditions on cases. The queue applies the very same
# conditions to the links the report gives it, so a row's count and the cases
# the queue then lists can never disagree.

_TERM = re.compile(r"^[0-9]{3,4}/[0-9]{1,2}$")


def parse_term(value: object) -> str:
    """'1448/1' as given, or '' for anything that is not a term."""
    text = str(value or "").strip()
    return text if _TERM.match(text) else ""


def term_q(term: str) -> Q:
    year, _, number = term.partition("/")
    return Q(academic_year=year, term=number)


def window_start(window: str, now: datetime) -> date:
    """The first day of a period of ``window`` days ending today, in whole local
    days: the period the timeline draws."""
    return timezone.localdate(now) - timedelta(days=int(window) - 1)


def sent_since_q(window: str, now: datetime) -> Q:
    """Cases sent on or after the first day of the period."""
    return Q(
        pk__in=TimetableFeedback.objects.annotate(_day=TruncDate("created_at"))
        .filter(_day__gte=window_start(window, now))
        .values("pk")
    )


def sent_from_programme_q(code: str) -> Q:
    """Cases sent while the student was in the programme ``code``."""
    return Q(
        pk__in=TimetableFeedback.objects.annotate(_programme=programme_code())
        .filter(_programme=code)
        .values("pk")
    )


def _split_identity(identity: str) -> tuple[str, str]:
    """('CS113', 'Programming') from 'CS113::Programming'; a bare code has no name."""
    code, _, name = str(identity or "").partition("::")
    return code.strip(), name.strip()


# ---------------------------------------------------------------------------
# What the report is narrowed to
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Filters:
    #: '1448/1', or '' for every term.
    term: str
    #: A programme code, upper-cased, or ''.
    programme: str
    #: A reason code, or ''.
    reason: str
    #: One of WINDOWS, or ''.
    window: str

    @classmethod
    def read(
        cls,
        query: Mapping[str, Any],
        *,
        terms: Sequence[str],
        programmes: Sequence[str],
    ) -> Filters:
        """The filters a query asks for, each one only if it is on offer. With no
        term asked for, the term being planned when it has feedback, else the
        latest term that has any."""
        asked_term = str(query.get("term") or "").strip()
        if asked_term == EVERY_TERM:
            term = ""
        elif asked_term in terms:
            term = asked_term
        else:
            # The feedback service's own name for the term, so the report and the
            # pages students write from can never disagree about which term it is.
            planning = term_key(*feedback.planning_term())
            term = planning if planning in terms else (terms[0] if terms else "")
        programme = str(query.get("programme") or "").strip().upper()
        reason = str(query.get("reason") or "").strip().upper()
        window = str(query.get("window") or "").strip()
        return cls(
            term=term,
            programme=programme if programme in programmes else "",
            reason=reason if reason in R.values else "",
            window=window if window in WINDOWS else "",
        )

    @property
    def term_value(self) -> str:
        """What the term select sends for these filters."""
        return self.term or EVERY_TERM


def terms_of(cases: QuerySet[TimetableFeedback]) -> list[str]:
    """The terms these cases are about, latest first."""
    pairs = cases.order_by().values_list("academic_year", "term").distinct()
    return [
        term_key(year, term)
        for year, term in sorted(set(pairs), key=lambda p: (str(p[0]), str(p[1])), reverse=True)
    ]


def programmes_of(cases: QuerySet[TimetableFeedback]) -> list[str]:
    """The programmes the students were in when they wrote, as codes."""
    return sorted(
        {
            str(value).strip().upper()
            for value in cases.order_by().values_list("program_at_submission", flat=True).distinct()
            if str(value or "").strip()
        }
    )


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Totals:
    students: int
    cases: int
    points: int
    open: int
    waiting_on_staff: int
    waiting_on_student: int
    with_department: int
    answered: int
    closed: int
    withdrawn: int
    #: Cases nobody on the staff has acted on, sent more than PROMPT ago.
    untouched: int
    #: Students who had a registration recorded when they wrote.
    registered_students: int
    #: Students expected to graduate, as their record says now.
    graduating_students: int


@dataclass(frozen=True)
class Response:
    #: Median hours from sending to the first staff action; None when no case has one.
    median_hours: float | None
    #: Cases with a first staff action within PROMPT, and with one at all.
    prompt: int
    acted_on: int
    helpful_yes: int
    helpful_no: int


@dataclass(frozen=True)
class ReasonRow:
    code: str
    students: int
    points: int


@dataclass(frozen=True)
class SectionRow:
    course_identity: str
    course_code: str
    course_name: str
    section: str
    students: int
    points: int
    #: (reason code, students), most students first.
    reasons: list[tuple[str, int]]
    #: (section asked for, students), most students first.
    moves: list[tuple[str, int]]
    registered_students: int


@dataclass(frozen=True)
class MoveRow:
    course_identity: str
    course_code: str
    course_name: str
    section_from: str
    section_to: str
    students: int


@dataclass(frozen=True)
class MissingRow:
    course_identity: str
    course_code: str
    course_name: str
    students: int
    programmes: list[str]


@dataclass(frozen=True)
class ProgrammeRow:
    programme: str
    students: int
    cases: int
    open: int
    waiting_on_staff: int
    answered: int
    top_reason: str


@dataclass(frozen=True)
class Report:
    filters: Filters
    terms: list[str]
    programmes: list[str]
    totals: Totals
    response: Response
    reasons: list[ReasonRow]
    sections: list[SectionRow]
    #: Sections raised beyond the SECTION_ROWS listed.
    sections_more: int
    moves: list[MoveRow]
    missing: list[MissingRow]
    by_programme: list[ProgrammeRow]
    #: (day, cases sent that day), oldest first, every day of the timeline.
    timeline: list[tuple[date, int]]
    #: Live points by what they are about: one section, the timetable, no timetable.
    anchors: dict[str, int] = field(default_factory=dict)
    #: Cases by status.
    by_status: dict[str, int] = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return self.totals.cases == 0


def _narrowed(
    staff: staff_service.Staff, filters: Filters, now: datetime
) -> tuple[
    QuerySet[TimetableFeedback], QuerySet[TimetableFeedback], QuerySet[TimetableFeedbackPoint]
]:
    """(cases, live cases, live points) the report counts: visible to ``staff``
    and within ``filters``. With an issue chosen, the cases that raise it and only
    the points about it.

    A case the student withdrew says nothing any more about what students ask:
    its points are left as they were (the workflow marks only a single point
    withdrawn), so it is set aside here. It still counts where the work is
    counted, by status and by day."""
    cases = staff_service.visible_to(staff)
    if filters.term:
        cases = cases.filter(term_q(filters.term))
    if filters.programme:
        cases = cases.filter(sent_from_programme_q(filters.programme))
    if filters.window:
        cases = cases.filter(sent_since_q(filters.window, now))
    points = TimetableFeedbackPoint.objects.filter(withdrawn_at__isnull=True)
    if filters.reason:
        points = points.filter(reason_code=filters.reason)
        cases = cases.filter(Exists(points.filter(case=OuterRef("pk"))))
    live = cases.exclude(status=S.WITHDRAWN)
    points = points.filter(case__in=live.values("pk"))
    return cases, live, points


def _totals(
    cases: QuerySet[TimetableFeedback],
    live: QuerySet[TimetableFeedback],
    points: QuerySet[TimetableFeedbackPoint],
    now: datetime,
) -> Totals:
    counted = cases.aggregate(
        cases=Count("pk"),
        open=Count("pk", filter=~Q(status__in=TIMETABLE_FEEDBACK_TERMINAL_STATUSES)),
        waiting_on_staff=Count("pk", filter=Q(status__in=staff_service.WAITING_ON_STAFF)),
        waiting_on_student=Count("pk", filter=Q(status=S.NEEDS_INFORMATION)),
        with_department=Count("pk", filter=Q(status=S.FORWARDED)),
        answered=Count("pk", filter=Q(status=S.ANSWERED)),
        closed=Count("pk", filter=Q(status=S.CLOSED)),
        withdrawn=Count("pk", filter=Q(status=S.WITHDRAWN)),
        untouched=Count(
            "pk",
            filter=Q(
                status__in=staff_service.WAITING_ON_STAFF,
                first_staff_action_at__isnull=True,
                created_at__lt=now - PROMPT,
            ),
        ),
    )
    # Who asked: students with feedback they have not withdrawn.
    people = live.aggregate(
        students=Count("student_id", distinct=True),
        registered_students=Count(
            "student_id", distinct=True, filter=Q(has_registration_at_submission=True)
        ),
    )
    graduating = (
        Student.objects.filter(
            student_id__in=live.values("student_id"), status=EXPECTED_GRADUATE_STATUS
        )
        .values("student_id")
        .distinct()
        .count()
    )
    return Totals(points=points.count(), graduating_students=graduating, **counted, **people)


def _response(cases: QuerySet[TimetableFeedback]) -> Response:
    waits = [
        (first - sent).total_seconds()
        for sent, first in cases.filter(first_staff_action_at__isnull=False).values_list(
            "created_at", "first_staff_action_at"
        )
    ]
    helpful = cases.aggregate(
        yes=Count("pk", filter=Q(answer_helpful=True)),
        no=Count("pk", filter=Q(answer_helpful=False)),
    )
    return Response(
        median_hours=statistics.median(waits) / 3600 if waits else None,
        prompt=sum(1 for seconds in waits if seconds <= PROMPT.total_seconds()),
        acted_on=len(waits),
        helpful_yes=helpful["yes"],
        helpful_no=helpful["no"],
    )


def _reasons(points: QuerySet[TimetableFeedbackPoint]) -> list[ReasonRow]:
    rows = (
        points.order_by()
        .values("reason_code")
        .annotate(students=Count("case__student_id", distinct=True), points=Count("pk"))
    )
    return sorted(
        (ReasonRow(r["reason_code"], r["students"], r["points"]) for r in rows),
        key=lambda row: (-row.students, -row.points, row.code),
    )


def _ranked(counter: Mapping[str, set[int]]) -> list[tuple[str, int]]:
    """(key, students) pairs, most students first, then by key."""
    return sorted(((key, len(ids)) for key, ids in counter.items()), key=lambda p: (-p[1], p[0]))


def _sections(points: QuerySet[TimetableFeedbackPoint]) -> tuple[list[SectionRow], int]:
    on_sections = points.filter(anchor_kind=TimetableFeedbackPoint.AnchorKind.SECTION)
    groups = list(
        on_sections.order_by()
        .values("section_course_identity", "section_label")
        .annotate(students=Count("case__student_id", distinct=True), points=Count("pk"))
    )
    groups.sort(
        key=lambda g: (
            -g["students"],
            -g["points"],
            g["section_course_identity"],
            g["section_label"],
        )
    )
    shown = groups[:SECTION_ROWS]
    wanted = Q(pk__in=[])
    for group in shown:
        wanted |= Q(
            section_course_identity=group["section_course_identity"],
            section_label=group["section_label"],
        )
    reasons: dict[tuple[str, str], dict[str, set[int]]] = defaultdict(lambda: defaultdict(set))
    moves: dict[tuple[str, str], dict[str, set[int]]] = defaultdict(lambda: defaultdict(set))
    registered: dict[tuple[str, str], set[int]] = defaultdict(set)
    for identity, label, reason, asked, asked_id, student, has_registration in on_sections.filter(
        wanted
    ).values_list(
        "section_course_identity",
        "section_label",
        "reason_code",
        "requested_section_label",
        "requested_term_section_id",
        "case__student_id",
        "case__has_registration_at_submission",
    ):
        key = (identity, label)
        reasons[key][reason].add(student)
        # A move names a section the form offered, as the moves table counts it.
        if reason == R.PREFER_OTHER_SECTION and asked and asked_id is not None:
            moves[key][asked].add(student)
        if has_registration:
            registered[key].add(student)
    rows = []
    for group in shown:
        key = (group["section_course_identity"], group["section_label"])
        code, name = _split_identity(key[0])
        rows.append(
            SectionRow(
                course_identity=key[0],
                course_code=code,
                course_name=name,
                section=key[1],
                students=group["students"],
                points=group["points"],
                reasons=_ranked(reasons[key]),
                moves=_ranked(moves[key])[:3],
                registered_students=len(registered[key]),
            )
        )
    return rows, max(0, len(groups) - len(shown))


def _moves(points: QuerySet[TimetableFeedbackPoint]) -> list[MoveRow]:
    rows = (
        points.filter(reason_code=R.PREFER_OTHER_SECTION, requested_term_section_id__isnull=False)
        .order_by()
        .values("section_course_identity", "section_label", "requested_section_label")
        .annotate(students=Count("case__student_id", distinct=True))
    )
    out = []
    for row in rows:
        code, name = _split_identity(row["section_course_identity"])
        out.append(
            MoveRow(
                course_identity=row["section_course_identity"],
                course_code=code,
                course_name=name,
                section_from=row["section_label"],
                section_to=row["requested_section_label"],
                students=row["students"],
            )
        )
    out.sort(key=lambda m: (-m.students, m.course_identity, m.section_from, m.section_to))
    return out[:MOVE_ROWS]


def _missing(points: QuerySet[TimetableFeedbackPoint]) -> list[MissingRow]:
    students: dict[str, set[int]] = defaultdict(set)
    programmes: dict[str, set[str]] = defaultdict(set)
    for identity, code, student, programme in (
        points.filter(reason_code=R.MISSING_COURSE)
        .exclude(requested_course_key="")
        .values_list(
            "requested_plan_course_identity",
            "requested_course_key",
            "case__student_id",
            "case__program_at_submission",
        )
    ):
        # Two courses can share a code; the plan's CODE::NAME tells them apart.
        key = identity or code
        students[key].add(student)
        if str(programme or "").strip():
            programmes[key].add(str(programme).strip().upper())
    names = _plan_course_names({_split_identity(key)[0] for key in students})
    out = []
    for key, ids in students.items():
        code, _key_name = _split_identity(key)
        out.append(
            MissingRow(
                course_identity=key,
                course_code=code,
                # The identity's name part is a normalised key; the plan's own
                # wording is what people read.
                course_name=names.get(key, ""),
                students=len(ids),
                programmes=sorted(programmes[key]),
            )
        )
    out.sort(key=lambda m: (-m.students, m.course_identity))
    return out[:MISSING_ROWS]


def _plan_course_names(codes: set[str]) -> dict[str, str]:
    """The study plans' own names for these codes, by plan identity (CODE::NAME)."""
    names: dict[str, str] = {}
    for code, name in (
        ProgrammeRequirement.objects.filter(course_code__in=codes)
        .order_by("program")
        .values_list("course_code", "course_name")
    ):
        if str(name or "").strip():
            names.setdefault(planner_course_key(code, name), str(name).strip())
    return names


def _by_programme(
    live: QuerySet[TimetableFeedback], points: QuerySet[TimetableFeedbackPoint]
) -> list[ProgrammeRow]:
    """Each programme's feedback, withdrawn cases aside. ``answered`` counts the
    cases that were given an answer at any time, closed since or not."""
    rows = (
        live.order_by()
        .annotate(_programme=programme_code())
        .values("_programme")
        .annotate(
            students=Count("student_id", distinct=True),
            cases=Count("pk"),
            open=Count("pk", filter=~Q(status__in=TIMETABLE_FEEDBACK_TERMINAL_STATUSES)),
            waiting_on_staff=Count("pk", filter=Q(status__in=staff_service.WAITING_ON_STAFF)),
            answered=Count("pk", filter=Q(answered_at__isnull=False)),
        )
    )
    top: dict[str, dict[str, set[int]]] = defaultdict(lambda: defaultdict(set))
    for programme, reason, student in points.values_list(
        "case__program_at_submission", "reason_code", "case__student_id"
    ):
        top[str(programme or "").strip().upper()][reason].add(student)
    out = []
    for row in rows:
        programme = str(row["_programme"] or "").strip()
        ranked = _ranked(top[programme])
        out.append(
            ProgrammeRow(
                programme=programme,
                students=row["students"],
                cases=row["cases"],
                open=row["open"],
                waiting_on_staff=row["waiting_on_staff"],
                answered=row["answered"],
                top_reason=ranked[0][0] if ranked else "",
            )
        )
    out.sort(key=lambda p: (-p.students, p.programme))
    return out


def timeline_days(filters: Filters) -> int:
    """The days the timeline draws: the chosen period, else TIMELINE_DAYS."""
    return int(filters.window) if filters.window else TIMELINE_DAYS


def _timeline(
    cases: QuerySet[TimetableFeedback], filters: Filters, now: datetime
) -> list[tuple[date, int]]:
    days = timeline_days(filters)
    # The same first day as the period filter, so with a period chosen the
    # timeline holds exactly the cases counted.
    first = window_start(str(days), now)
    counts = {
        row["day"]: row["n"]
        for row in cases.order_by()
        .annotate(day=TruncDate("created_at"))
        .filter(day__gte=first)
        .values("day")
        .annotate(n=Count("pk"))
    }
    return [
        (first + timedelta(days=n), counts.get(first + timedelta(days=n), 0)) for n in range(days)
    ]


def build(
    staff: staff_service.Staff, query: Mapping[str, Any], *, now: datetime | None = None
) -> Report:
    """The report for ``staff``, narrowed by the filters ``query`` asks for."""
    now = now or timezone.now()
    visible = staff_service.visible_to(staff)
    terms = terms_of(visible)
    programmes = programmes_of(visible)
    filters = Filters.read(query, terms=terms, programmes=programmes)
    cases, live, points = _narrowed(staff, filters, now)
    sections, sections_more = _sections(points)
    anchors = {
        row["anchor_kind"]: row["n"]
        for row in points.order_by().values("anchor_kind").annotate(n=Count("pk"))
    }
    return Report(
        filters=filters,
        terms=terms,
        programmes=programmes,
        totals=_totals(cases, live, points, now),
        response=_response(cases),
        reasons=_reasons(points),
        sections=sections,
        sections_more=sections_more,
        moves=_moves(points),
        missing=_missing(points),
        by_programme=_by_programme(live, points),
        timeline=_timeline(cases, filters, now),
        anchors=anchors,
        by_status={
            row["status"]: row["n"]
            for row in cases.order_by().values("status").annotate(n=Count("pk"))
        },
    )

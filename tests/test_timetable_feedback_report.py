"""The timetable feedback report: what it counts, over whose cases, and the page.

Counted at the service (`timetable_feedback_report.build`) and read through the
real URL, view and template. Every case here is built directly, as the department
board will meet it, so each count can be named exactly.
"""

from __future__ import annotations

import itertools
import re
from datetime import datetime, time, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from django.test import Client
from django.utils import timezone
from django.utils.html import escape

from core.models import ProgrammeRequirement, Student, TimetableFeedback, TimetableFeedbackPoint
from core.services import timetable_feedback as feedback
from core.services import timetable_feedback_copy as copy
from core.services import timetable_feedback_report as report
from core.services import timetable_feedback_staff as staff_service
from core.services import timetable_feedback_staff_copy as scopy
from core.services.course_identity import planner_course_key
from core.services.credit_policy import EXPECTED_GRADUATE_STATUS
from core.services.rbac import ROLE_ADVISOR, ROLE_GENERAL_ADVISOR, ROLE_SUPER_ADMIN
from tests.test_timetable_feedback_staff import (
    ADV,
    ADV_B,
    ELSEWHERE,
    PROG,
    S_BLANK,
    S_MESSY,
    S_MINE,
    S_OTHER_ADVISOR,
    S_OTHER_PROG,
    S_RAW_NAME,
    S_TABBED,
    make_students,
    staff_user,
)

pytestmark = pytest.mark.django_db

REPORT = "/ops/timetable-feedback/report/"
QUEUE = "/ops/timetable-feedback/"
YEAR, TERM = "1448", "1"
_IDS = itertools.count(1)
#: Reasons about the timetable as a whole; every other one is about a section.
WHOLE_TIMETABLE = {
    "LOAD_TOO_HIGH",
    "LOAD_TOO_LOW",
    "MISSING_COURSE",
    "TOO_MANY_CAMPUS_DAYS",
    "GRADUATION_CONCERN",
}


@pytest.fixture(autouse=True)
def _feedback_on(settings, monkeypatch):
    settings.TIMETABLE_FEEDBACK_ENABLED = True
    monkeypatch.setattr(feedback, "planning_term", lambda: (YEAR, TERM))


@pytest.fixture
def people(db):
    make_students()
    return {
        "advisor": staff_user("rep-advisor", ROLE_ADVISOR, advisor_id=ADV),
        "other": staff_user("rep-other", ROLE_ADVISOR, advisor_id=ADV_B),
        "no_id": staff_user("rep-no-id", ROLE_ADVISOR),
        "department": staff_user("rep-dept", ROLE_GENERAL_ADVISOR, departments=PROG),
        "admin": staff_user("rep-admin", ROLE_SUPER_ADMIN),
    }


def _case(sid, status="SUBMITTED", *, year=YEAR, term=TERM, programme=PROG, ago=None, **fields):
    """A case as it would stand in ``status``; ``ago`` backdates when it was sent."""
    if status == "CLOSED":
        fields.setdefault("close_reason", "OTHER")
    case = TimetableFeedback.objects.create(
        student_id=sid,
        academic_year=year,
        term=term,
        status=status,
        program_at_submission=programme,
        **fields,
    )
    if ago is not None:
        TimetableFeedback.objects.filter(pk=case.pk).update(created_at=timezone.now() - ago)
        case.refresh_from_db()
    return case


def _point(
    case,
    reason="TIME_CLASH",
    *,
    course="SA101::Systems Analysis",
    section="M1",
    asked="",
    missing="",
    missing_identity="",
    withdrawn=False,
):
    """One live point. A section point names a PHYSICAL section by its course
    (CODE::NAME) and label, as the board groups them."""
    n = next(_IDS)
    fields: dict[str, object] = {}
    if reason in WHOLE_TIMETABLE:
        fields.update(anchor_kind="TIMETABLE", anchor_key="TIMETABLE")
    else:
        section_id = 70_000 + n
        fields.update(
            anchor_kind="SECTION",
            term_section_id=section_id,
            anchor_key=f"SECTION|{section_id}",
            section_label=section,
            section_course_identity=course,
            course_key=course.partition("::")[0],
        )
    if asked:
        asked_id = 90_000 + n
        fields.update(
            requested_term_section_id=asked_id,
            requested_section_label=asked,
            request_key=f"SECTION|{asked_id}",
        )
    if missing:
        fields.update(
            requested_course_key=missing,
            requested_plan_course_identity=missing_identity or missing,
            request_key=f"COURSE|{missing}",
        )
    return TimetableFeedbackPoint.objects.create(
        case=case,
        reason_code=reason,
        seen_fingerprint=f"{n:064d}",
        withdrawn_at=timezone.now() if withdrawn else None,
        **fields,
    )


def _build(user, **query):
    return report.build(staff_service.staff_of(user), query)


def _reasons(data):
    return {row.code: (row.students, row.points) for row in data.reasons}


# ---------------------------------------------------------------------------
# Whose cases
# ---------------------------------------------------------------------------


def test_each_person_counts_exactly_the_cases_they_can_open(people):
    mine = _case(S_MINE)
    theirs = _case(S_OTHER_ADVISOR)
    assigned = _case(S_OTHER_ADVISOR, term="2", assigned_advisor_id=ADV)
    elsewhere = _case(S_OTHER_PROG, programme=ELSEWHERE)
    for case in (mine, theirs, assigned, elsewhere):
        _point(case)

    expected = {
        # Their own students (one of them in another programme) and the case the
        # department assigned them.
        "advisor": {mine.pk, assigned.pk, elsewhere.pk},
        "other": {theirs.pk, assigned.pk},
        # The department sees its programme, whoever advises.
        "department": {mine.pk, theirs.pk, assigned.pk},
        "admin": {mine.pk, theirs.pk, assigned.pk, elsewhere.pk},
        "no_id": set(),
    }
    for who, cases in expected.items():
        staff = staff_service.staff_of(people[who])
        # The report's scope IS the queue's: the same cases, never more.
        assert set(staff_service.visible_to(staff).values_list("pk", flat=True)) == cases, who
        data = _build(people[who], term=report.EVERY_TERM)
        assert data.totals.cases == len(cases), who
        assert data.totals.points == len(cases), who
    assert _build(people["no_id"]).empty


# ---------------------------------------------------------------------------
# What is counted
# ---------------------------------------------------------------------------


def test_a_student_counts_once_per_issue_and_a_withdrawn_point_not_at_all(people):
    first = _case(S_MINE)
    _point(first, "TIME_CLASH", section="M1")
    _point(first, "TIME_CLASH", section="M2")
    _point(first, "PREFER_OTHER_SECTION", asked="M3", withdrawn=True)
    second = _case(S_MESSY)
    _point(second, "TIME_CLASH")

    data = _build(people["admin"])
    assert _reasons(data) == {"TIME_CLASH": (2, 3)}
    assert data.totals.points == 3
    assert data.totals.students == 2
    assert data.moves == []


def test_sections_group_the_physical_section_and_rank_what_was_asked_for(people):
    programming = "CS113::Programming"
    for sid, asked in ((S_MINE, "M31"), (S_MESSY, "M31"), (S_TABBED, "M35")):
        _point(_case(sid), "PREFER_OTHER_SECTION", course=programming, section="M33", asked=asked)
    clash = TimetableFeedback.objects.get(student_id=S_MINE)
    _point(clash, "TIME_CLASH", course=programming, section="M33")
    _point(_case(S_OTHER_ADVISOR), "TIME_UNSUITABLE", course=programming, section="M34")
    # The same code, another course: a section of it is never one of the first's.
    _point(_case(S_RAW_NAME), "TIME_CLASH", course="CS113::Something Else", section="M33")

    rows = {(row.course_identity, row.section): row for row in _build(people["admin"]).sections}
    top = rows[(programming, "M33")]
    assert (top.students, top.points) == (3, 4)
    assert top.reasons == [("PREFER_OTHER_SECTION", 3), ("TIME_CLASH", 1)]
    assert top.moves == [("M31", 2), ("M35", 1)]
    assert (top.course_code, top.course_name) == ("CS113", "Programming")
    assert rows[(programming, "M34")].students == 1
    assert rows[("CS113::Something Else", "M33")].students == 1
    first = _build(people["admin"]).sections[0]
    assert (first.course_identity, first.section) == (programming, "M33")


def test_a_withdrawn_case_says_nothing_of_what_students_ask_but_counts_as_work(people):
    withdrawn = _case(S_MINE, "WITHDRAWN")
    _point(withdrawn, "PREFER_OTHER_SECTION", section="M1", asked="M2")
    _point(withdrawn, "MISSING_COURSE", missing="MA101")
    _point(_case(S_MESSY), "TIME_CLASH", section="M1")

    data = _build(people["admin"])
    assert _reasons(data) == {"TIME_CLASH": (1, 1)}
    assert (data.totals.students, data.totals.points) == (1, 1)
    assert [(row.section, row.students, row.moves) for row in data.sections] == [("M1", 1, [])]
    assert data.moves == [] and data.missing == []
    assert [(row.programme, row.students, row.cases) for row in data.by_programme] == [(PROG, 1, 1)]
    # Still a case sent, and counted where it is.
    assert (data.totals.cases, data.totals.live_cases, data.totals.withdrawn) == (2, 1, 1)
    assert data.by_status["WITHDRAWN"] == 1
    assert sum(n for _day, n in data.timeline) == 2
    # The programme table adds up to the cases that still say something, and so
    # does the key figure's own count of cases.
    assert sum(row.cases for row in data.by_programme) == data.totals.live_cases
    hint = scopy.text("kpi_students_hint", "en", cases=copy.bdi(1), points=copy.bdi(1))
    assert str(hint) in _page(people["admin"])


def test_the_queue_opened_from_the_report_sets_withdrawn_feedback_aside_too(people):
    withdrawn = _case(S_MINE, "WITHDRAWN")
    _point(withdrawn, "TIME_CLASH", course="CS113::Programming", section="M33")
    standing = _case(S_MESSY)
    _point(standing, "TIME_CLASH", course="CS113::Programming", section="M33")

    html = _page(people["admin"], term=report.EVERY_TERM)
    (row,) = _build(people["admin"], term=report.EVERY_TERM).sections
    assert row.students == 1
    for wanted in ({"section": "M33"}, {"reason": "TIME_CLASH"}):
        link = _link_with(html, **wanted)
        page, listed = _listed(_as(people["admin"]), link)
        assert listed == {standing.reference}, wanted
        assert 'id="ttfsReportFilter"' in page
    # Filtering again from that page keeps the mark, so nothing comes back.
    page = _as(people["admin"]).get(_link_with(html, reason="TIME_CLASH")).content.decode()
    form = page.split('id="ttfsFilters"')[1].split("</form>")[0]
    assert '<input type="hidden" name="from" value="report">' in form
    # Without the report's mark, the queue lists both, as it always did.
    plain = _as(people["admin"]).get(QUEUE, {"tab": "all", "reason": "TIME_CLASH"})
    assert {withdrawn.reference, standing.reference} <= set(
        re.findall(r'href="/ops/timetable-feedback/(TTF-\d{4}-\d{5})/"', plain.content.decode())
    )


@pytest.mark.parametrize(
    ("value", "read"),
    [("1448/1", "1448/1"), ("1448/S", "1448/S"), ("1448", ""), ("1448/1/2", ""), ("x y/1", "")],
)
def test_the_queue_reads_a_term_only_in_the_reports_own_form(value, read):
    assert report.parse_term(value) == read


def test_a_move_is_only_a_section_the_form_offered(people):
    case = _case(S_MINE)
    point = _point(case, "PREFER_OTHER_SECTION", section="M1")
    # A label with no offered section behind it is no move.
    TimetableFeedbackPoint.objects.filter(pk=point.pk).update(requested_section_label="M9")
    data = _build(people["admin"])
    assert data.sections[0].moves == [] and data.moves == []


def test_the_period_is_whole_local_days_as_the_timeline_draws_them(people):
    today = timezone.localdate()
    early_in_the_first_day = timezone.make_aware(
        datetime.combine(today - timedelta(days=6), time(0, 30))
    )
    late_the_day_before = timezone.make_aware(
        datetime.combine(today - timedelta(days=7), time(23, 30))
    )
    for sid, sent in ((S_MINE, early_in_the_first_day), (S_MESSY, late_the_day_before)):
        case = _case(sid)
        TimetableFeedback.objects.filter(pk=case.pk).update(created_at=sent)
    data = _build(people["admin"], window="7")
    assert data.totals.cases == 1
    assert data.timeline[0] == (today - timedelta(days=6), 1)
    assert sum(n for _day, n in data.timeline) == data.totals.cases


def test_the_timeline_says_which_days_it_draws(people):
    _point(_case(S_MINE))
    assert "Over the last 30 days" in _page(people["admin"])
    assert "Over the last 7 days" in _page(people["admin"], window="7")


def test_the_busiest_day_is_a_date_kept_on_one_line(people):
    _point(_case(S_MINE))
    today = timezone.localdate().isoformat()
    assert f'busiest day <bdi dir="ltr" class="ttf-date">{today}</bdi>' in _page(people["admin"])


def test_a_section_row_counts_students_already_registered(people):
    for sid, registered in ((S_MINE, True), (S_MESSY, False)):
        case = _case(sid, has_registration_at_submission=registered)
        _point(case, "TIME_CLASH", section="M9")
    (row,) = _build(people["admin"]).sections
    assert (row.students, row.registered_students) == (2, 1)


def test_the_sections_beyond_the_listed_ones_are_counted_not_listed(people, monkeypatch):
    monkeypatch.setattr(report, "SECTION_ROWS", 2)
    case = _case(S_MINE)
    for label in ("M1", "M2", "M3", "M4"):
        _point(case, "TIME_CLASH", section=label)
    data = _build(people["admin"])
    assert len(data.sections) == 2 and data.sections_more == 2


def test_section_moves_are_listed_from_and_to_by_students(people):
    for sid in (S_MINE, S_MESSY):
        _point(
            _case(sid), "PREFER_OTHER_SECTION", course="MA101::Calculus", section="F2", asked="F1"
        )
    _point(
        _case(S_TABBED), "PREFER_OTHER_SECTION", course="MA101::Calculus", section="F2", asked="F3"
    )
    # Another reason naming no section is no move.
    _point(_case(S_OTHER_ADVISOR), "TIME_CLASH", course="MA101::Calculus", section="F2")

    moves = [
        (m.course_code, m.section_from, m.section_to, m.students)
        for m in _build(people["admin"]).moves
    ]
    assert moves == [("MA101", "F2", "F1", 2), ("MA101", "F2", "F3", 1)]


def test_missing_courses_keep_two_courses_of_one_code_apart_and_read_the_plans_name(people):
    ProgrammeRequirement.objects.create(
        program=PROG, course_code="MA201", course_name="Linear Algebra"
    )
    ProgrammeRequirement.objects.create(
        program=ELSEWHERE, course_code="MA201", course_name="Discrete Mathematics"
    )
    algebra = planner_course_key("MA201", "Linear Algebra")
    discrete = planner_course_key("MA201", "Discrete Mathematics")
    for sid in (S_MINE, S_MESSY):
        _point(_case(sid), "MISSING_COURSE", missing="MA201", missing_identity=algebra)
    _point(
        _case(S_OTHER_PROG, programme=ELSEWHERE),
        "MISSING_COURSE",
        missing="MA201",
        missing_identity=discrete,
    )

    rows = {row.course_identity: row for row in _build(people["admin"]).missing}
    assert (rows[algebra].students, rows[algebra].course_name) == (2, "Linear Algebra")
    assert rows[algebra].programmes == [PROG]
    assert (rows[discrete].students, rows[discrete].course_name) == (1, "Discrete Mathematics")
    assert rows[discrete].programmes == [ELSEWHERE]


def test_by_programme_counts_the_programme_written_in_and_its_top_issue(people):
    _point(_case(S_MINE, programme=PROG), "TIME_CLASH")
    _point(_case(S_MESSY, programme=" fbs "), "TIME_CLASH")
    _point(_case(S_TABBED, "ANSWERED", programme=PROG, answered_at=timezone.now()), "LOAD_TOO_HIGH")
    _point(_case(S_OTHER_PROG, programme=ELSEWHERE), "GRADUATION_CONCERN")

    rows = {row.programme: row for row in _build(people["admin"]).by_programme}
    # " fbs " is FBS: spaces and case left by an import do not split a programme.
    assert set(rows) == {PROG, ELSEWHERE}
    fbs = rows[PROG]
    assert (fbs.students, fbs.cases, fbs.open, fbs.waiting_on_staff, fbs.answered) == (
        3,
        3,
        3,
        2,
        1,
    )
    assert fbs.top_reason == "TIME_CLASH"
    assert rows[ELSEWHERE].top_reason == "GRADUATION_CONCERN"


def test_the_work_figures(people):
    now = timezone.now()
    # Untouched: still waiting on staff, nobody acted, sent more than two days ago.
    _point(_case(S_MINE, ago=timedelta(days=3)))
    # Too recent to count as untouched.
    _point(_case(S_MESSY, ago=timedelta(days=1)))
    # Acted on within two hours, and in three days.
    prompt = _case(S_TABBED, "IN_REVIEW", ago=timedelta(days=5))
    TimetableFeedback.objects.filter(pk=prompt.pk).update(
        first_staff_action_at=prompt.created_at + timedelta(hours=2)
    )
    slow = _case(S_OTHER_ADVISOR, "ANSWERED", ago=timedelta(days=6), answer_helpful=True)
    TimetableFeedback.objects.filter(pk=slow.pk).update(
        first_staff_action_at=slow.created_at + timedelta(hours=72)
    )
    _case(S_RAW_NAME, "CLOSED", answer_helpful=False, first_staff_action_at=now)
    _case(S_BLANK, "NEEDS_INFORMATION", first_staff_action_at=now)
    # A second helpful answer, so helpful and not helpful can never pass swapped.
    _case(S_MINE, "CLOSED", term="2", answer_helpful=True)

    data = _build(people["admin"], term=report.EVERY_TERM)
    totals, response = data.totals, data.response
    assert totals.untouched == 1
    assert (totals.waiting_on_staff, totals.waiting_on_student) == (3, 1)
    assert (totals.answered, totals.closed, totals.open) == (1, 2, 5)
    assert response.acted_on == 4
    assert response.prompt == 3
    # The two acted on before "now": 2 h and 72 h; the two acted on just now
    # count as soon as they were sent (no wait at all).
    assert response.median_hours == pytest.approx(1.0, abs=0.05)
    assert (response.helpful_yes, response.helpful_no) == (2, 1)
    assert data.by_status == {
        "SUBMITTED": 2,
        "IN_REVIEW": 1,
        "ANSWERED": 1,
        "CLOSED": 2,
        "NEEDS_INFORMATION": 1,
    }


def test_students_expected_to_graduate_are_counted_from_their_record_now(people):
    _case(S_MINE)
    _case(S_MESSY)
    Student.objects.filter(student_id=S_MINE).update(status=EXPECTED_GRADUATE_STATUS)
    assert _build(people["admin"]).totals.graduating_students == 1


def test_the_timeline_has_every_day_and_each_case_on_its_own_day(people):
    # The same moment for both, so a run at midnight cannot part them.
    _case(S_MINE, ago=timedelta(days=2))
    _case(S_MESSY, ago=timedelta(days=2))
    _case(S_TABBED, ago=timedelta(days=40))
    timeline = _build(people["admin"]).timeline
    assert len(timeline) == report.TIMELINE_DAYS
    assert timeline[-1][0] == timezone.localdate()
    counts = dict(timeline)
    two_days_ago = timezone.localdate(timezone.now() - timedelta(days=2))
    assert counts[two_days_ago] == 2
    assert sum(counts.values()) == 2  # the case 40 days back is outside the window
    week = _build(people["admin"], window="7").timeline
    assert len(week) == 7


# ---------------------------------------------------------------------------
# Narrowing the report
# ---------------------------------------------------------------------------


def test_the_term_is_the_one_being_planned_unless_another_is_chosen(people):
    _point(_case(S_MINE, term="1"))
    _point(_case(S_MESSY, term="2"))
    _point(_case(S_TABBED, year="1447", term="3"))

    planned = _build(people["admin"])
    assert planned.filters.term == "1448/1" and planned.totals.cases == 1
    assert planned.terms == ["1448/2", "1448/1", "1447/3"]
    assert _build(people["admin"], term="1448/2").totals.cases == 1
    assert _build(people["admin"], term=report.EVERY_TERM).totals.cases == 3
    # A term with no feedback, or a made-up one, is the planned term again.
    assert _build(people["admin"], term="1999/9").filters.term == "1448/1"


def test_with_no_feedback_in_the_planned_term_the_latest_term_is_shown(people, monkeypatch):
    monkeypatch.setattr(feedback, "planning_term", lambda: ("1450", "1"))
    _point(_case(S_MINE, term="2"))
    _point(_case(S_MESSY, year="1447", term="1"))
    assert _build(people["admin"]).filters.term == "1448/2"


def test_an_issue_narrows_to_the_cases_raising_it_and_only_its_points(people):
    both = _case(S_MINE)
    _point(both, "TIME_CLASH")
    _point(both, "LOAD_TOO_HIGH")
    _point(_case(S_MESSY), "LOAD_TOO_HIGH")

    data = _build(people["admin"], reason="TIME_CLASH")
    assert data.filters.reason == "TIME_CLASH"
    assert (data.totals.cases, data.totals.points) == (1, 1)
    assert _reasons(data) == {"TIME_CLASH": (1, 1)}
    # Anything that is not a reason narrows nothing.
    assert _build(people["admin"], reason="DROP TABLE").totals.cases == 2


def test_a_programme_narrows_to_what_its_students_wrote_from_it(people):
    _point(_case(S_MINE, programme=" fbs "))
    _point(_case(S_OTHER_PROG, programme=ELSEWHERE))
    data = _build(people["admin"], programme="fbs")
    assert data.filters.programme == PROG and data.totals.cases == 1
    assert data.programmes == sorted([PROG, ELSEWHERE])
    # A programme the person cannot see is no filter at all.
    assert _build(people["advisor"], programme="CS").filters.programme == ""


def test_a_period_counts_only_what_was_sent_within_it(people):
    _point(_case(S_MINE, ago=timedelta(days=3)))
    _point(_case(S_MESSY, ago=timedelta(days=10)))
    assert _build(people["admin"], window="7").totals.cases == 1
    assert _build(people["admin"], window="30").totals.cases == 2
    assert _build(people["admin"], window="5").filters.window == ""


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------


def _as(user):
    client = Client()
    client.force_login(user)
    return client


def _page(user, language="en", **params):
    response = _as(user).get(REPORT, params, headers={"accept-language": language})
    assert response.status_code == 200
    return response.content.decode()


@pytest.mark.parametrize("language", ["en", "ar"])
def test_the_report_page_draws_every_part_in_the_language_asked_for(people, language):
    case = _case(S_MINE, has_registration_at_submission=True)
    _point(case, "PREFER_OTHER_SECTION", course="CS113::Programming", section="M33", asked="M31")
    _point(case, "MISSING_COURSE", missing="MA201", missing_identity="MA201")
    html = _page(people["advisor"], language)
    t = scopy.strings(language)
    assert f'<html lang="{language}"' in html
    for key in (
        "report_title",
        "kpi_students",
        "report_reasons_title",
        "report_sections_title",
        "report_moves_title",
        "report_missing_title",
        "report_programmes_title",
        "report_timeline_title",
    ):
        assert escape(t[key]) in html, key
    for element in ("ttfrKpi-students", "ttfrReasons", "ttfrSectionsTable", "ttfrMovesTable"):
        assert f'id="{element}"' in html, element
    assert escape(scopy.reason_label("PREFER_OTHER_SECTION", language)) in html
    # The pages' own navigation marks the report as the current page.
    assert re.search(r'id="ttfsNavReport" aria-current="page"', html)


def test_an_advisor_never_sees_another_advisors_cases_in_the_report(people):
    _point(_case(S_MINE), "TIME_CLASH", course="ZZ100::Mine Only", section="Q1")
    _point(_case(S_OTHER_ADVISOR), "TIME_CLASH", course="ZZ900::Not Theirs", section="Q9")
    advisor = _page(people["advisor"])
    assert "ZZ100" in advisor and "ZZ900" not in advisor
    department = _page(people["department"])
    assert "ZZ100" in department and "ZZ900" in department


def test_the_scope_line_says_whose_cases_were_counted(people):
    _point(_case(S_MINE))
    assert escape(scopy.strings("en")["report_scope_advisor"]) in _page(people["advisor"])
    assert escape(scopy.strings("en")["report_scope_admin"]) in _page(people["admin"])
    assert PROG in _page(people["department"]).split('id="ttfrScope"')[1].split("</p>")[0]


def test_with_nothing_to_count_the_report_says_so(people):
    html = _page(people["advisor"])
    assert 'id="ttfrEmpty"' in html
    assert escape(scopy.strings("en")["report_empty"]) in html
    assert 'id="ttfrKpi-students"' not in html


def _links(html, prefix=QUEUE):
    return [
        href.replace("&amp;", "&")
        for href in re.findall(r'href="([^"]+)"', html)
        if href.startswith(prefix + "?")
    ]


def test_a_section_row_opens_the_queue_on_exactly_its_cases(people):
    raised = _case(S_MINE)
    _point(raised, "TIME_CLASH", course="CS113::Programming", section="M33")
    other_section = _case(S_MESSY)
    _point(other_section, "TIME_CLASH", course="CS113::Programming", section="M34")
    other_course = _case(S_TABBED)
    _point(other_course, "TIME_CLASH", course="CS113::Something Else", section="M33")

    links = [
        link
        for link in _links(_page(people["department"]))
        if parse_qs(urlsplit(link).query).get("section") == ["M33"]
        and parse_qs(urlsplit(link).query).get("course") == ["CS113::Programming"]
    ]
    assert links, "no link to the section's cases"
    response = _as(people["department"]).get(links[0])
    html = response.content.decode()
    listed = set(re.findall(r'href="/ops/timetable-feedback/(TTF-\d{4}-\d{5})/"', html))
    assert listed == {raised.reference}
    assert 'id="ttfsReportFilter"' in html


def test_a_reason_row_opens_the_queue_on_that_issue(people):
    _point(_case(S_MINE), "LOAD_TOO_HIGH")
    links = _links(_page(people["advisor"]))
    assert any(parse_qs(urlsplit(link).query).get("reason") == ["LOAD_TOO_HIGH"] for link in links)


def test_the_queue_reads_a_section_only_with_its_course(people):
    case = _case(S_MINE)
    _point(case, "TIME_CLASH", course="CS113::Programming", section="M33")
    other = _case(S_MESSY)
    _point(other, "TIME_CLASH", course="CS113::Programming", section="M34")
    client = _as(people["department"])
    alone = client.get(QUEUE, {"tab": "all", "section": "M33"}).content.decode()
    # A label alone names a section of any course: it narrows nothing.
    assert 'id="ttfsReportFilter"' not in alone
    assert {case.reference, other.reference} <= set(
        re.findall(r'href="/ops/timetable-feedback/(TTF-\d{4}-\d{5})/"', alone)
    )


def _listed(client, link):
    html = client.get(link).content.decode()
    return html, set(re.findall(r'href="/ops/timetable-feedback/(TTF-\d{4}-\d{5})/"', html))


def _link_with(html, **wanted):
    for link in _links(html):
        query = parse_qs(urlsplit(link).query)
        if all(query.get(key) == [value] for key, value in wanted.items()):
            return link
    raise AssertionError(f"no link with {wanted}")


def test_a_row_link_carries_every_narrowing_and_lands_on_exactly_the_counted_cases(people):
    counted = _case(S_MINE, programme=PROG)
    _point(counted, "TIME_CLASH", course="CS113::Programming", section="M33")
    # Each of these differs from the narrowing in exactly one way.
    other_term = _case(S_MESSY, term="2", programme=PROG)
    _point(other_term, "TIME_CLASH", course="CS113::Programming", section="M33")
    too_old = _case(S_TABBED, programme=PROG, ago=timedelta(days=10))
    _point(too_old, "TIME_CLASH", course="CS113::Programming", section="M33")
    other_programme = _case(S_OTHER_ADVISOR, programme=ELSEWHERE)
    _point(other_programme, "TIME_CLASH", course="CS113::Programming", section="M33")
    other_issue = _case(S_RAW_NAME, programme=PROG)
    _point(other_issue, "TIME_UNSUITABLE", course="CS113::Programming", section="M33")

    narrowing = {"term": "1448/1", "window": "7", "programme": PROG, "reason": "TIME_CLASH"}
    html = _page(people["admin"], **narrowing)
    (row,) = _build(people["admin"], **narrowing).sections
    assert row.students == 1
    link = _link_with(html, course="CS113::Programming", section="M33", term="1448/1", window="7")
    assert parse_qs(urlsplit(link).query)["sent_programme"] == [PROG]
    page, listed = _listed(_as(people["admin"]), link)
    assert listed == {counted.reference}
    notice = page.split('id="ttfsReportFilter"')[1].split("</div>")[0]
    for part in ("1448/1", PROG, "CS113", "M33"):
        assert part in notice, part
    # And the notice's own link takes every narrowing off again.
    clear = re.search(r'<a href="\?([^"]*)">', notice).group(1).replace("&amp;", "&")
    assert not {"term", "window", "sent_programme", "course", "section"} & set(parse_qs(clear))


def test_a_section_link_with_an_issue_needs_one_point_raising_both(people):
    elsewhere = _case(S_MINE)
    _point(elsewhere, "PREFER_OTHER_SECTION", course="CS113::Programming", section="M33")
    _point(elsewhere, "TIME_CLASH", course="CS113::Programming", section="M34")
    here = _case(S_MESSY)
    _point(here, "TIME_CLASH", course="CS113::Programming", section="M33")

    html = _page(people["admin"], reason="TIME_CLASH")
    link = _link_with(html, course="CS113::Programming", section="M33", reason="TIME_CLASH")
    # The first case raises the issue and names the section, but on two points.
    assert _listed(_as(people["admin"]), link)[1] == {here.reference}


def test_a_programme_row_opens_the_cases_sent_from_it_wherever_the_student_is_now(people):
    moved_since = _case(S_OTHER_PROG, programme=PROG)  # in FBX now, wrote from FBS
    _point(moved_since)
    stayed = _case(S_MINE, programme=PROG)
    _point(stayed)
    html = _page(people["admin"])
    link = _link_with(html, sent_programme=PROG)
    assert _listed(_as(people["admin"]), link)[1] == {moved_since.reference, stayed.reference}


def test_each_section_link_says_which_section_it_opens(people):
    _point(_case(S_MINE), "TIME_CLASH", course="CS113::Programming", section="M33")
    _point(_case(S_MESSY), "TIME_CLASH", course="MA101::Calculus", section="F2")
    html = _page(people["admin"])
    for what in ("CS113 M33", "MA101 F2"):
        label = str(scopy.text("report_see_cases_about", "en", what=copy.bdi(what)))
        assert label in html, what

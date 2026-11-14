"""The student's timetable-feedback pages, through the real URLs, views and templates.

Every wording assertion runs in a language forced by the request, and an Arabic one
first proves the page really rendered in Arabic: an Arabic string asserted against an
English page asserts nothing.
"""

from __future__ import annotations

import re
from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from core.models import (
    ProgrammeRequirement,
    Student,
    StudentTermSection,
    TermSectionMeeting,
    TimetableFeedback,
    TimetableFeedbackMessage,
    TimetableFeedbackPoint,
    TimetableScenario,
    UserScope,
)
from core.services import rate_limit, student_otp
from core.services import timetable_feedback as feedback
from core.services import timetable_feedback_copy as copy
from core.services.rbac import ensure_role_groups
from core.services.student_term_timetable import TimetableUnavailable
from tests.test_timetable_feedback_submission import _section as _submission_section

pytestmark = pytest.mark.django_db

SID = 4994001
EMPTY_SID = 4994002
REG_SID = 4994003
OTHER_SID = 4994004
PROG = "FBV"
YEAR = "1448"
TERM = "1"
PLAN = "registration_plan_1448_t1"
FINGERPRINT = re.compile(r'name="seen_fingerprint" value="([0-9a-f]{64})"')


def _section(*args, **kwargs):
    """The submission tests' helper, linked to THIS module's programme: its own
    default is that module's, which would leave every section outside the plan."""
    kwargs.setdefault("programme", PROG)
    return _submission_section(*args, **kwargs)


@pytest.fixture(autouse=True)
def _flags(settings, monkeypatch):
    settings.TIMETABLE_FEEDBACK_ENABLED = True
    settings.TIMETABLE_FEEDBACK_ACCEPTING = True
    settings.TIMETABLE_FEEDBACK_PROGRAMMES = ()
    # The home page and the feedback service each read the term through their own
    # name; both must name the same term, or every point is refused as changed.
    monkeypatch.setattr(feedback, "planning_term", lambda: (YEAR, TERM))
    monkeypatch.setattr(
        "core.student_auth_views.load_defaults",
        lambda: {"academic_year": 1448, "term": 1, "currentYear": 1448, "currentTerm": 1},
    )
    for budget in (
        rate_limit.TIMETABLE_FEEDBACK_ATTEMPT,
        rate_limit.TIMETABLE_FEEDBACK,
        rate_limit.TIMETABLE_FEEDBACK_DAILY,
    ):
        monkeypatch.setitem(rate_limit.LIMITS, budget, (100, 3600))


@pytest.fixture
def world(db):
    for sid in (SID, EMPTY_SID, REG_SID, OTHER_SID):
        Student.objects.create(
            student_id=sid,
            name=f"Student {sid}",
            program=PROG,
            section="M",
            status="منتظم",
            advisor_id="ADV-7",
            total_earned_credits=60,
            current_registered_credits=0,
        )
    for code, name in {
        "SA101": "INTRO TO PROGRAMMING",
        "SB201": "DATA STRUCTURES",
        "SC201": "DISCRETE MATHEMATICS",
    }.items():
        ProgrammeRequirement.objects.create(
            program=PROG,
            course_code=code,
            course_name=name,
            type="Mandatory",
            programme_term=1,
            credit_hours=3,
        )
    scenario = TimetableScenario.objects.create(academic_year=YEAR, term=TERM, name="draft")
    intro = "INTRO TO PROGRAMMING"
    s = {
        "SA101-M1": _section("SA101", "M1", [("SUN", "09:00", "10:15", "A1")], name=intro),
        "SA101-M2": _section("SA101", "M2", [("MON", "13:00", "14:15", "B1")], name=intro),
        "SA101-F1": _section("SA101", "F1", [("MON", "13:00", "14:15", "F1")], name=intro),
        "SA101-M3": _section(
            "SA101", "M3", [("WED", "13:00", "14:15", "B3")], name=intro, programme=None
        ),
        "SA101-M4": _section(
            "SA101", "M4", [("WED", "13:00", "14:15", "B4")], name=intro, scenario=scenario
        ),
        "SB201-M1": _section(
            "SB201", "M1", [("SUN", "09:30", "10:45", "B2")], name="DATA STRUCTURES"
        ),
        "SC201-M1": _section(
            "SC201", "M1", [("WED", "08:00", "09:15", "C3")], name="DISCRETE MATHEMATICS"
        ),
        "SN000-M1": _section("SN000", "M1", [], name="NO TIMES YET"),
        # In the forecast, but with no section label: it counts towards the
        # fingerprint and can never be chosen.
        "SX900-blank": _section("SX900", "", [("THU", "15:00", "16:15", "X1")], name="UNLABELLED"),
    }
    for key in ("SA101-M1", "SB201-M1", "SN000-M1", "SX900-blank"):
        StudentTermSection.objects.create(
            student_id=SID, academic_year=YEAR, term=TERM, term_section=s[key], source=PLAN
        )
    StudentTermSection.objects.create(
        student_id=OTHER_SID, academic_year=YEAR, term=TERM, term_section=s["SA101-M1"], source=PLAN
    )
    StudentTermSection.objects.create(
        student_id=REG_SID,
        academic_year=YEAR,
        term=TERM,
        term_section=s["SA101-M2"],
        source="scraper_timetable",
    )
    return s


def _client(sid=SID, **kwargs):
    ensure_role_groups()
    client = Client(**kwargs)
    client.force_login(student_otp.provision_student_user(sid))
    return client


def _get(client, url, language="en", **params):
    return client.get(url, params, headers={"accept-language": language})


def _post(client, url, data, language="en"):
    return client.post(url, data, headers={"accept-language": language})


def _html(response):
    return response.content.decode()


def _assert_language(html, language):
    assert f'<html lang="{language}"' in html, "the page did not render in that language"


NEW = "/student/timetable-feedback/new/"
LIST = "/student/timetable-feedback/"


def _describe(client, world, about="SA101-M1", language="en"):
    """Step two for a section of the forecast; returns (response, fingerprint)."""
    anchor = "TIMETABLE" if about == "TIMETABLE" else f"SECTION|{world[about].pk}"
    response = _get(client, NEW, language, about=anchor)
    match = FINGERPRINT.search(_html(response))
    return response, (match.group(1) if match else ""), anchor


def _send(client, world, about="SA101-M1", language="en", **fields):
    _, fingerprint, anchor = _describe(client, world, about, language)
    data = {"anchor": anchor, "seen_fingerprint": fingerprint, "reason": "TIME_CLASH", "note": ""}
    data.update(fields)
    return _post(client, NEW, data, language)


def _detail_url(reference):
    return reverse("student_timetable_feedback_detail", args=[reference])


def test_the_routes_are_where_the_pages_link_to():
    assert reverse("student_timetable_feedback_list") == LIST
    assert reverse("student_timetable_feedback_new") == NEW
    assert _detail_url("TTF-2026-00001") == "/student/timetable-feedback/TTF-2026-00001/"


# ---------------------------------------------------------------------------
# Who may see what
# ---------------------------------------------------------------------------

PAGES = [LIST, NEW, "/student/timetable-feedback/TTF-2026-00001/"]


@pytest.mark.parametrize("url", PAGES)
def test_an_anonymous_visitor_is_sent_to_the_student_login(url):
    response = Client().get(url)
    assert response.status_code == 302
    assert response["Location"].startswith(reverse("student_login"))
    assert "next=" in response["Location"]


@pytest.mark.parametrize("url", PAGES)
def test_staff_are_sent_to_their_dashboard(world, url):
    staff = User.objects.create_user("advisor-ttf", password="x")
    client = Client()
    client.force_login(staff)
    response = client.get(url)
    assert response.status_code == 302 and response["Location"] == reverse("dashboard")


@pytest.mark.parametrize("url", PAGES)
def test_a_student_account_not_linked_to_a_record_gets_the_portal_409(world, url):
    client = _client()
    UserScope.objects.filter(user__username=str(SID)).update(student_id=None)
    assert client.get(url).status_code == 409


@pytest.mark.parametrize("url", PAGES)
@pytest.mark.parametrize("language", ["en", "ar"])
def test_with_the_feature_off_every_page_is_absent(world, settings, url, language):
    client = _client()
    if url.startswith("/student/timetable-feedback/TTF-"):
        # A case that really exists, so only the switch can hide it.
        url = _send(client, world)["Location"]
    settings.TIMETABLE_FEEDBACK_ENABLED = False
    response = _get(client, url, language)
    html = _html(response)
    assert response.status_code == 404
    _assert_language(html, language)
    assert copy.strings(language)["unavailable_title"] in html


@pytest.mark.parametrize("url", PAGES)
def test_no_feedback_page_is_ever_cached(world, url):
    response = _get(_client(), url)
    assert "no-store" in response["Cache-Control"]


@pytest.mark.parametrize("language", ["en", "ar"])
def test_with_feedback_closed_the_form_says_so_but_cases_stay_readable(world, settings, language):
    client = _client()
    reference = _send(client, world)["Location"].rstrip("/").rsplit("/", 1)[1]
    settings.TIMETABLE_FEEDBACK_ACCEPTING = False

    closed = _get(client, NEW, language)
    assert closed.status_code == 403
    _assert_language(_html(closed), language)
    assert str(copy.refusal("NOT_ACCEPTING", language)) in _html(closed)
    assert _get(client, LIST, language).status_code == 200
    detail = _get(client, _detail_url(reference), language)
    assert detail.status_code == 200
    assert copy.strings(language)["detail_add_point"] not in _html(detail)


def test_a_programme_outside_the_pilot_cannot_open_the_form(world, settings):
    settings.TIMETABLE_FEEDBACK_PROGRAMMES = ("OTHER",)
    assert _get(_client(), NEW).status_code == 403


@pytest.mark.parametrize("language", ["en", "ar"])
def test_another_students_case_reads_exactly_like_one_that_does_not_exist(world, language):
    theirs = feedback.submit_point(
        student_id=OTHER_SID,
        anchor=f"SECTION|{world['SA101-M1'].pk}",
        reason="TIME_CLASH",
        seen_fingerprint=_fingerprint(OTHER_SID),
    )
    client = _client()
    other = _get(client, _detail_url(theirs.reference), language)
    malformed = _get(client, "/student/timetable-feedback/not-a-reference/", language)
    assert other.status_code == malformed.status_code == 404
    sentence = str(copy.refusal("CASE_NOT_FOUND", language))
    assert sentence in _html(other) and sentence in _html(malformed)
    # The page itself says nothing about that case. (The URL the student typed is
    # echoed only by the sidebar's language switcher, outside <main>.)
    main = _html(other).split("<main", 1)[1].split("</main>", 1)[0]
    assert theirs.reference not in main


def _fingerprint(sid=SID, language="en"):
    from core.services.student_term_timetable import read_student_term_timetable

    timetable = read_student_term_timetable(sid, YEAR, TERM)
    sections = feedback.seen_sections(timetable, programme=PROG, language=language)
    return feedback.forecast_fingerprint(
        academic_year=YEAR, term=TERM, forecast_class=timetable.forecast_class, sections=sections
    )


def test_a_post_without_the_csrf_token_is_refused(world):
    client = _client(enforce_csrf_checks=True)
    _, fingerprint, anchor = _describe(client, world)
    response = client.post(
        NEW, {"anchor": anchor, "seen_fingerprint": fingerprint, "reason": "TIME_CLASH"}
    )
    assert response.status_code == 403
    assert not TimetableFeedbackPoint.objects.exists()


# ---------------------------------------------------------------------------
# The home page
# ---------------------------------------------------------------------------


def _home(sid=SID, language="en"):
    return _html(_get(_client(sid), reverse("student_home"), language))


@pytest.mark.parametrize("language", ["en", "ar"])
def test_the_expected_card_offers_feedback_on_itself(world, language):
    html = _home(language=language)
    _assert_language(html, language)
    card = html.split('id="student-home-timetable-expected"', 1)[1].split("</section>", 1)[0]
    assert copy.strings(language)["home_send"] in card
    assert f'href="{NEW}"' in card
    # Nothing is registered for this student, so nothing says it is.
    assert "already recorded" not in html and "سبق تسجيلك" not in html


def test_an_open_case_from_another_term_is_not_put_on_this_terms_card(world):
    client = _client()
    _send(client, world)
    TimetableFeedback.objects.filter(student_id=SID).update(academic_year="1447", term="2")
    html = _html(_get(client, reverse("student_home")))
    assert copy.strings("en")["home_send"] in html
    assert "View my feedback" not in html


def test_a_registered_student_is_told_changes_are_made_in_the_portal(world):
    StudentTermSection.objects.create(
        student_id=SID,
        academic_year=YEAR,
        term=TERM,
        term_section=world["SC201-M1"],
        source="scraper_timetable",
    )
    html = _home()
    assert "already recorded" in html and "university portal" in html


def test_an_open_case_is_shown_with_its_status_and_a_way_to_add_to_it(world):
    client = _client()
    _send(client, world)
    case = TimetableFeedback.objects.get(student_id=SID)
    html = _html(_get(client, reverse("student_home")))
    assert f'href="{_detail_url(case.reference)}"' in html
    assert case.reference in html and "Status: Sent" in html
    assert copy.strings("en")["home_add_point"] in html
    assert copy.strings("en")["home_send"] not in html


@pytest.mark.parametrize("language", ["en", "ar"])
def test_a_student_with_no_timetable_at_all_may_say_so(world, language):
    html = _home(EMPTY_SID, language)
    _assert_language(html, language)
    assert 'id="student-home-feedback-no-forecast"' in html
    assert copy.strings(language)["no_forecast_button"] in html


def test_a_registered_only_student_is_offered_nothing_that_could_only_fail(world):
    html = _home(REG_SID)
    assert 'id="student-home-feedback"' not in html
    assert 'id="student-home-feedback-no-forecast"' not in html


def test_a_timetable_that_could_not_be_read_is_not_called_unprepared(world, monkeypatch):
    def broken(*args, **kwargs):
        raise TimetableUnavailable("store down")

    monkeypatch.setattr("core.student_auth_views.read_student_term_timetable", broken)
    html = _home(EMPTY_SID)
    assert 'id="student-home-feedback-no-forecast"' not in html


def test_no_button_is_put_on_another_terms_fallback_panel(db):
    """The configured term has nothing; the only published timetable is 1447/2.
    Its panel is shown, but feedback is about 1448/1, so it carries no button."""
    Student.objects.create(student_id=SID, name="Fallback", program=PROG, section="M")
    old = _section("SA101", "M1", [("SUN", "09:00", "10:15", "A1")], name="INTRO")
    StudentTermSection.objects.create(
        student_id=SID,
        academic_year="1447",
        term="2",
        term_section=old,
        source="registration_plan_1447_t2",
    )
    html = _home()
    assert 'id="student-home-timetable-expected"' in html
    assert 'id="student-home-feedback"' not in html


@pytest.mark.parametrize(
    ("setting", "value"),
    [("TIMETABLE_FEEDBACK_ENABLED", False), ("TIMETABLE_FEEDBACK_ACCEPTING", False)],
)
def test_the_home_page_offers_nothing_new_while_feedback_is_closed(world, settings, setting, value):
    setattr(settings, setting, value)
    html = _home()
    assert copy.strings("en")["home_send"] not in html
    assert copy.strings("en")["no_forecast_button"] not in _home(EMPTY_SID)


# ---------------------------------------------------------------------------
# The form, step one
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("language", ["en", "ar"])
def test_step_one_lists_the_expected_sections_and_the_whole_timetable(world, language):
    response = _get(_client(), NEW, language)
    html = _html(response)
    assert response.status_code == 200
    _assert_language(html, language)
    offered = set(re.findall(r'name="about" value="([^"]+)"', html))
    assert offered == {
        f"SECTION|{world['SA101-M1'].pk}",
        f"SECTION|{world['SB201-M1'].pk}",
        f"SECTION|{world['SN000-M1'].pk}",
        "TIMETABLE",
    }
    # SA101-M1 and SB201-M1 overlap on Sunday morning.
    assert html.count(copy.strings(language)["new_clash_hint"]) == 2
    assert copy.strings(language)["new_no_times"] in html
    assert copy.day_label("SUN", language) in html


def test_a_section_without_a_label_is_never_offered_nor_accepted(world):
    client = _client()
    assert f"SECTION|{world['SX900-blank'].pk}" not in _html(_get(client, NEW))
    response = _get(client, NEW, about=f"SECTION|{world['SX900-blank'].pk}")
    assert response.status_code == 400


def test_an_unknown_choice_returns_to_step_one_with_the_reason(world):
    response = _get(_client(), NEW, about=f"SECTION|{world['SA101-M2'].pk}")
    html = _html(response)
    assert response.status_code == 400
    assert 'role="alert"' in html and copy.strings("en")["new_about_required"] in html
    assert 'aria-invalid="true"' in html


@pytest.mark.parametrize("language", ["en", "ar"])
def test_a_student_with_nothing_prepared_gets_the_one_thing_they_can_send(world, language):
    html = _html(_get(_client(EMPTY_SID), NEW, language))
    assert 'name="anchor" value="NO_FORECAST"' in html
    assert copy.strings(language)["new_no_forecast_submit"] in html


def test_a_registered_only_student_is_told_there_is_nothing_to_send(world):
    html = _html(_get(_client(REG_SID), NEW))
    assert 'name="anchor"' not in html
    assert "managed in the university portal" in html


def test_a_timetable_that_could_not_be_read_stops_the_form(world, monkeypatch):
    def broken(*args, **kwargs):
        raise TimetableUnavailable("store down")

    monkeypatch.setattr("core.student_timetable_feedback_views.read_student_term_timetable", broken)
    response = _get(_client(), NEW)
    assert response.status_code == 503
    assert str(copy.refusal("TIMETABLE_UNAVAILABLE", "en")) in _html(response)


# ---------------------------------------------------------------------------
# The form, step two
# ---------------------------------------------------------------------------


def test_step_two_for_a_section_offers_its_issues_and_only_real_alternatives(world):
    response, fingerprint, _ = _describe(_client(), world)
    html = _html(response)
    reasons = re.findall(r'name="reason" value="([A-Z_]+)"', html)
    assert reasons == [
        code
        for code in (
            "TIME_CLASH",
            "TIME_UNSUITABLE",
            "PREFER_OTHER_SECTION",
            "ALREADY_PASSED",
            "PREREQUISITE_NOT_MET",
            "NOT_IN_MY_PLAN",
            "OTHER",
        )
    ]
    offered = set(re.findall(r'name="requested_section" value="(\d+)"', html))
    # Not the section itself, not the other cohort's, not one without the programme,
    # not a draft scenario's.
    assert offered == {str(world["SA101-M2"].pk)}
    assert fingerprint == _fingerprint()


def test_a_section_without_times_may_say_so(world):
    html = _html(_describe(_client(), world, "SN000-M1")[0])
    assert 'value="SECTION_HAS_NO_TIMES"' in html


def test_step_two_for_the_timetable_offers_only_courses_that_can_be_listed(world):
    html = _html(_describe(_client(), world, "TIMETABLE")[0])
    assert re.findall(r'name="requested_course" value="([A-Z0-9]+)"', html) == ["SC201"]
    assert 'value="MISSING_COURSE"' in html and 'value="TIME_CLASH"' not in html


@pytest.mark.parametrize("language", ["en", "ar"])
def test_the_note_says_who_reads_it_and_how_long_it_may_be(world, language):
    html = _html(_describe(_client(), world, language=language)[0])
    assert copy.strings(language)["new_privacy"] in html
    assert f'maxlength="{feedback.NOTE_MAX_CHARS}"' in html


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("language", ["en", "ar"])
def test_a_sent_point_lands_on_its_case_which_says_so_once(world, language):
    client = _client()
    response = _send(client, world, language=language, note="Clashes on Sunday")
    case = TimetableFeedback.objects.get(student_id=SID)
    assert response.status_code == 302 and response["Location"] == _detail_url(case.reference)

    first = _html(_get(client, response["Location"], language))
    _assert_language(first, language)
    assert str(copy.notice("SENT", language, reference=case.reference)) in first
    assert "Clashes on Sunday" in first
    again = _html(_get(client, response["Location"], language))
    assert str(copy.notice("SENT", language, reference=case.reference)) not in again


def test_sending_the_same_point_twice_adds_nothing_and_says_so(world):
    client = _client()
    _send(client, world, note="same")
    response = _send(client, world, note="same")
    html = _html(_get(client, response["Location"]))
    case = TimetableFeedback.objects.get(student_id=SID)
    assert case.points.count() == 1
    assert str(copy.notice("DUPLICATE", "en", reference=case.reference)) in html


def test_a_point_that_reopens_an_answer_says_so(world):
    client = _client()
    _send(client, world)
    TimetableFeedback.objects.filter(student_id=SID).update(
        status="ANSWERED", answered_at=timezone.now()
    )
    response = _send(client, world, reason="TIME_UNSUITABLE")
    html = _html(_get(client, response["Location"]))
    assert "reopened this feedback" in html


@pytest.mark.parametrize(
    ("fields", "field", "key"),
    [
        ({"reason": ""}, "reason", "new_reason_required"),
        ({"reason": "PREFER_OTHER_SECTION"}, "requested_section", "new_requested_required"),
    ],
)
def test_a_missing_choice_is_named_on_its_own_field(world, fields, field, key):
    response = _send(_client(), world, **fields)
    html = _html(response)
    assert response.status_code == 400
    assert copy.strings("en")[key] in html
    error_id = re.search(r'<li id="(ttfError-\d+)"', html).group(1)
    assert re.search(rf'aria-describedby="[^"]*{error_id}[^"]*" aria-invalid="true"', html)
    assert not TimetableFeedbackPoint.objects.exists()


def test_a_missing_course_must_be_named(world):
    response = _send(_client(), world, about="TIMETABLE", reason="MISSING_COURSE")
    assert response.status_code == 400
    assert copy.strings("en")["new_missing_required"] in _html(response)


def test_a_section_left_chosen_for_an_issue_that_does_not_use_it_is_ignored(world):
    response = _send(
        _client(), world, reason="ALREADY_PASSED", requested_section=str(world["SA101-M2"].pk)
    )
    assert response.status_code == 302
    point = TimetableFeedbackPoint.objects.get()
    assert (point.request_key, point.requested_term_section_id) == ("", None)


def test_a_section_the_form_did_not_offer_is_refused_without_naming_it(world):
    response = _send(
        _client(), world, reason="TIME_UNSUITABLE", requested_section=str(world["SA101-F1"].pk)
    )
    html = _html(response)
    assert response.status_code == 400
    assert str(copy.refusal("NOT_OFFERED", "en")) in html
    summary = html.split('id="ttfErrors"', 1)[1].split("</div>", 1)[0]
    assert "F1" not in summary


def test_a_timetable_that_changed_while_writing_is_drawn_again(world):
    client = _client()
    _, old_fingerprint, anchor = _describe(client, world)
    TermSectionMeeting.objects.filter(term_section=world["SB201-M1"]).update(start_time="11:00")
    response = _post(
        client, NEW, {"anchor": anchor, "seen_fingerprint": old_fingerprint, "reason": "TIME_CLASH"}
    )
    html = _html(response)
    assert response.status_code == 409
    assert str(copy.refusal("TIMETABLE_CHANGED", "en")) in html
    assert FINGERPRINT.search(html).group(1) == _fingerprint() != old_fingerprint
    # The chosen section is still there, so the student stays on step two.
    assert f'name="anchor" value="{anchor}"' in html


def test_a_timetable_that_changes_after_the_page_read_it_is_drawn_from_a_new_read(
    world, monkeypatch
):
    """The page reads the timetable, then the service reads it again. A change in
    between is refused by the service; the form must be drawn from what is there
    now, not from the page's own earlier read, or the next try is refused too."""
    client = _client()
    _, fingerprint, anchor = _describe(client, world)
    real_submit = feedback.submit_point

    def changed_in_between(**kwargs):
        TermSectionMeeting.objects.filter(term_section=world["SB201-M1"]).update(start_time="11:00")
        return real_submit(**kwargs)

    monkeypatch.setattr(feedback, "submit_point", changed_in_between)
    response = _post(
        client, NEW, {"anchor": anchor, "seen_fingerprint": fingerprint, "reason": "TIME_CLASH"}
    )
    assert response.status_code == 409
    assert FINGERPRINT.search(_html(response)).group(1) == _fingerprint() != fingerprint


def test_a_point_already_raised_with_other_words_links_to_its_case(world):
    client = _client()
    _send(client, world, note="first")
    response = _send(client, world, note="second")
    case = TimetableFeedback.objects.get(student_id=SID)
    html = _html(response)
    assert response.status_code == 409
    assert f'href="{_detail_url(case.reference)}"' in html
    assert case.points.count() == 1


def test_a_student_who_sends_too_fast_is_told_how_long_to_wait(world, monkeypatch):
    monkeypatch.setitem(rate_limit.LIMITS, rate_limit.TIMETABLE_FEEDBACK, (1, 3600))
    client = _client()
    _send(client, world)
    response = _send(client, world, reason="TIME_UNSUITABLE")
    assert response.status_code == 429
    wait = int(response["Retry-After"])
    assert 0 < wait <= 3600
    assert copy.wait_text(wait, "en") in _html(response)


def test_line_breaks_count_once_however_the_browser_sent_them(world):
    """A textarea posts each line break as CR LF; the limit counts characters the
    student typed, so 95 lines of 9 letters fit although CR LF makes them 1,045."""
    note = ("a" * 9 + "\r\n") * 95
    response = _send(_client(), world, note=note)
    assert response.status_code == 302
    assert "\r" not in TimetableFeedbackPoint.objects.get().note


def test_the_note_is_required_for_something_else_and_marked_when_it_is_missing(world):
    response = _send(_client(), world, reason="OTHER", note="   ")
    html = _html(response)
    assert response.status_code == 400
    assert str(copy.refusal("NOTE_REQUIRED", "en")) in html
    assert re.search(r'id="ttfNote"[^>]*aria-invalid="true"', html)


@pytest.mark.parametrize("language", ["en", "ar"])
def test_saying_no_timetable_was_prepared_is_sent_once_and_only_by_those_without_one(
    world, language
):
    client = _client(EMPTY_SID)
    fingerprint = FINGERPRINT.search(_html(_get(client, NEW, language))).group(1)
    sent = _post(client, NEW, {"anchor": "NO_FORECAST", "seen_fingerprint": fingerprint}, language)
    assert sent.status_code == 302
    assert TimetableFeedbackPoint.objects.get().reason_code == "NO_FORECAST"

    # Someone WITH an expected timetable cannot send it.
    refused = _post(
        _client(), NEW, {"anchor": "NO_FORECAST", "seen_fingerprint": _fingerprint()}, language
    )
    assert refused.status_code == 400
    assert str(copy.refusal("NO_FORECAST_NOT_ALLOWED", language)) in _html(refused)


# ---------------------------------------------------------------------------
# One case
# ---------------------------------------------------------------------------


def test_a_case_shows_the_section_as_it_was_sent_not_as_it_is_now(world):
    client = _client()
    response = _send(client, world)
    TermSectionMeeting.objects.filter(term_section=world["SA101-M1"]).update(
        start_time="15:00", end_time="16:15"
    )
    html = _html(_get(client, response["Location"]))
    assert "09:00–10:15" in html and "15:00" not in html
    assert "SA101" in html and "M1" in html


def _case_with_messages(world):
    client = _client()
    location = _send(client, world)["Location"]
    case = TimetableFeedback.objects.get(student_id=SID)
    TimetableFeedback.objects.filter(pk=case.pk).update(handled_by_username="handler.sentinel")
    TimetableFeedbackMessage.objects.create(
        case=case,
        audience="STUDENT_VISIBLE",
        author_kind="ADVISOR",
        author_username="author.sentinel",
        author_label="Label Sentinel",
        body="Visible reply sentinel",
    )
    TimetableFeedbackMessage.objects.create(
        case=case,
        audience="INTERNAL",
        author_kind="ADVISOR",
        body="Internal note sentinel",
    )
    return client, location


@pytest.mark.parametrize("language", ["en", "ar"])
def test_only_what_the_student_may_read_reaches_the_page(world, language):
    client, location = _case_with_messages(world)
    html = _html(_get(client, location, language))
    assert "Visible reply sentinel" in html
    assert copy.author_label("ADVISOR", language) in html
    for secret in (
        "Internal note sentinel",
        "author.sentinel",
        "Label Sentinel",
        "handler.sentinel",
    ):
        assert secret not in html, secret


def test_a_case_from_another_term_is_shown_as_it_was_with_nothing_to_add(world):
    client = _client()
    location = _send(client, world)["Location"]
    TimetableFeedback.objects.filter(student_id=SID).update(academic_year="1447", term="2")
    html = _html(_get(client, location))
    assert "no longer the term being planned" in html
    assert copy.strings("en")["detail_add_point"] not in html


def test_a_closed_case_says_why_and_takes_nothing_new(world):
    client = _client()
    location = _send(client, world)["Location"]
    TimetableFeedback.objects.filter(student_id=SID).update(
        status="CLOSED", close_reason="DUPLICATE", closed_at=timezone.now()
    )
    html = _html(_get(client, location))
    assert copy.close_reason_label("DUPLICATE", "en") in html
    assert copy.strings("en")["detail_add_point"] not in html


def test_a_withdrawn_point_is_marked(world):
    client = _client()
    location = _send(client, world)["Location"]
    TimetableFeedbackPoint.objects.update(withdrawn_at=timezone.now() - timedelta(minutes=1))
    html = _html(_get(client, location))
    assert 'class="ttf-point is-withdrawn"' in html
    assert copy.strings("en")["detail_withdrawn"] in html


def test_the_list_shows_each_case_with_its_status_and_live_points(world):
    client = _client()
    _send(client, world)
    _send(client, world, reason="TIME_UNSUITABLE")
    case = TimetableFeedback.objects.get(student_id=SID)
    html = _html(_get(client, LIST))
    assert f'href="{_detail_url(case.reference)}"' in html
    assert "Points: " in html and '<bdi dir="ltr">2</bdi>' in html
    assert copy.status_label("SUBMITTED", "en") in html
    # A withdrawn point no longer counts.
    TimetableFeedbackPoint.objects.filter(reason_code="TIME_UNSUITABLE").update(
        withdrawn_at=timezone.now()
    )
    assert '<bdi dir="ltr">1</bdi>' in _html(_get(client, LIST))


def test_an_empty_list_says_so_and_offers_the_form(world):
    html = _html(_get(_client(), LIST))
    assert copy.strings("en")["list_empty"] in html
    assert f'href="{NEW}"' in html


# ---------------------------------------------------------------------------
# The sidebar, and the pages as a whole
# ---------------------------------------------------------------------------


def test_the_sidebar_link_shows_for_students_and_marks_every_feedback_page(world):
    client = _client()
    home = _html(_get(client, reverse("student_home")))
    assert f'href="{LIST}"' in home and 'aria-current="page"' not in home
    for url in (LIST, NEW):
        html = _html(_get(client, url))
        assert re.search(
            rf'class="nav-a nav-link active" href="{LIST}"[^>]*aria-current="page"', html
        )


def test_the_sidebar_link_is_absent_while_the_feature_is_off(world, settings):
    settings.TIMETABLE_FEEDBACK_ENABLED = False
    assert f'href="{LIST}"' not in _home()


def test_staff_never_get_the_student_link(world):
    from core.sidebar_context import get_sidebar_context

    staff = User.objects.create_superuser("admin-ttf", password="x")
    request = type("Request", (), {"user": staff})()
    assert get_sidebar_context(request)["can_timetable_feedback"] is False


@pytest.mark.parametrize("language", ["en", "ar"])
def test_no_page_leaks_template_syntax(world, language):
    client = _client()
    location = _send(client, world)["Location"]
    for url in (LIST, NEW, location, f"{NEW}?about=TIMETABLE"):
        html = _html(_get(client, url, language))
        for token in ("{%", "%}", "{{", "}}", "{#", "#}"):
            assert token not in html, (url, token)

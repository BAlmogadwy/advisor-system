"""Student "what can I take / why is it locked" report + screen."""

import pytest
from django.contrib.auth.models import User
from django.test import Client, override_settings

from core.models import Course, Prerequisite, ProgrammeRequirement, Student, StudentCourse
from core.services import student_otp
from core.services.eligibility import hour_gate, split_hour_prereqs
from core.services.rbac import ensure_role_groups, set_user_scope
from core.services.student_unlock import build_unlock_report

SID = 4930001
PROG = "TSTP"
pytestmark = pytest.mark.django_db


@pytest.fixture
def plan():
    """A tiny programme: A -> B -> C, plus CAP gated on 100 credit hours."""
    ensure_role_groups()
    Student.objects.update_or_create(
        student_id=SID,
        defaults={
            "name": "Unlock Test",
            "program": PROG,
            "section": "M",
            "total_earned_credits": 100,
            "current_registered_credits": 0,
        },
    )
    for code, name, term in (
        ("TA101", "Alpha", 1),
        ("TB201", "Beta", 3),
        ("TC301", "Gamma", 5),
        ("TCAP", "Capstone", 9),
    ):
        Course.objects.update_or_create(
            course_code=code, defaults={"description": name, "credit_hours": 3}
        )
        ProgrammeRequirement.objects.update_or_create(
            program=PROG,
            course_code=code,
            defaults={"programme_term": term, "credit_hours": 3, "type": "Mandatory"},
        )
    Prerequisite.objects.update_or_create(
        program=PROG, course_code="TB201", prerequisite_course_code="TA101"
    )
    Prerequisite.objects.update_or_create(
        program=PROG, course_code="TC301", prerequisite_course_code="TB201"
    )
    Prerequisite.objects.update_or_create(
        program=PROG, course_code="TCAP", prerequisite_course_code="100(HOURS)"
    )
    yield


def _report():
    return build_unlock_report(SID, 1448, 1)


# ── the credit-hour gate (this was a live bug: "100(HOURS)" tested as a course code) ──


def test_split_hour_prereqs_separates_the_gate():
    courses, hours = split_hour_prereqs(["CS101", "146(HOURS)", "CS102"])
    assert courses == ["CS101", "CS102"] and hours == 146
    assert split_hour_prereqs(["CS101"]) == (["CS101"], 0)


def test_hour_gate_counts_registered_credits(plan):
    g = hour_gate(SID, 100)
    assert g["met"] is True and g["effective"] == 100
    assert hour_gate(SID, 120)["met"] is False
    assert hour_gate(SID, 120)["remaining"] == 20
    # strict mode ignores in-progress credits
    Student.objects.filter(student_id=SID).update(
        total_earned_credits=90, current_registered_credits=15
    )
    assert hour_gate(SID, 100)["met"] is True
    assert hour_gate(SID, 100, strict_passed_only=True)["met"] is False


def test_capstone_with_met_hours_is_open_not_locked(plan):
    """Regression: an hour-gated course was permanently locked for everyone."""
    r = _report()
    assert "TCAP" in [c["code"] for c in r["open_courses"]]
    assert "TCAP" not in [c["code"] for c in r["locked_courses"]]


def test_capstone_with_unmet_hours_explains_hours_not_a_fake_course(plan):
    Student.objects.filter(student_id=SID).update(
        total_earned_credits=40, current_registered_credits=0
    )
    r = _report()
    cap = next(c for c in r["locked_courses"] if c["code"] == "TCAP")
    kinds = [x["kind"] for x in cap["reasons"]]
    assert kinds == ["MISSING_HOURS"]
    assert cap["hours_only"] is True
    assert cap["steps"] is None  # no course chain -> never claim "1 step"
    hrs = cap["reasons"][0]
    assert hrs["required"] == 100 and hrs["remaining"] == 60
    # the raw "100(HOURS)" string is never presented as a course
    assert all(x.get("code") != "100(HOURS)" for x in cap["reasons"])


# ── the chain ──


def test_chain_steps_reasons_and_nearest(plan):
    r = _report()
    codes_open = [c["code"] for c in r["open_courses"]]
    assert "TA101" in codes_open  # no prereqs -> open
    locked = {c["code"]: c for c in r["locked_courses"]}
    assert locked["TB201"]["steps"] == 2  # pass A, then B
    assert locked["TC301"]["steps"] == 3  # A, B, then C
    b_reason = locked["TB201"]["reasons"][0]
    assert b_reason["kind"] == "MISSING_COURSE" and b_reason["code"] == "TA101"
    assert b_reason["own_status"] == "open"  # the blocker is takeable now
    # the deepest course points at the nearest thing she can actually do
    assert locked["TC301"]["nearest_open"]["code"] == "TA101"
    assert r["counts"]["one_step"] == 1  # only TB201 is one course away


def test_passing_a_course_unlocks_the_next(plan):
    StudentCourse.objects.update_or_create(
        student_id=SID,
        course=Course.objects.get(course_code="TA101"),
        defaults={"status": "passed", "programme_term": 1},
    )
    r = _report()
    assert "TB201" in [c["code"] for c in r["open_courses"]]
    assert "TA101" in [c["code"] for c in r["done"]]
    assert r["counts"]["passed"] == 1


def test_top_blocker_is_the_course_that_frees_most(plan):
    r = _report()
    assert r["top_blocker"]["code"] == "TA101"  # frees B and C
    assert r["top_blocker"]["frees_eventually"] == 2


def test_open_and_locked_never_overlap(plan):
    r = _report()
    assert not ({c["code"] for c in r["open_courses"]} & {c["code"] for c in r["locked_courses"]})
    assert r["counts"]["open"] == len(r["open_courses"])
    assert r["counts"]["locked"] == len(r["locked_courses"])


# ── the screen ──


@override_settings(ALLOWED_HOSTS=["testserver"])
def test_screen_renders_from_session_identity_only(plan):
    u = student_otp.provision_student_user(SID)
    c = Client()
    c.force_login(u)
    r = c.get("/student/courses/")
    assert r.status_code == 200
    assert r.context["student_id"] == SID
    # a client-supplied id must not change whose report is built
    assert c.get("/student/courses/?student_id=4930002").context["student_id"] == SID
    body = r.content.decode()
    assert "TA101" in body
    for undefined in ("text-muted", "text-bg-warning", "table-light"):
        assert undefined not in body  # classes that do not exist in this design system


@override_settings(ALLOWED_HOSTS=["testserver"])
def test_staff_are_redirected_off_the_student_screen(plan):
    staff = User.objects.create_user(username="adv77", password="x", is_staff=True)
    set_user_scope(staff.id, advisor_id="A1")
    c = Client()
    c.force_login(staff)
    assert c.get("/student/courses/").status_code == 302


@override_settings(ALLOWED_HOSTS=["testserver"])
def test_screen_survives_a_builder_failure(plan, monkeypatch):
    monkeypatch.setattr(
        "core.student_auth_views.build_unlock_report",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    u = student_otp.provision_student_user(SID)
    c = Client()
    c.force_login(u)
    r = c.get("/student/courses/")
    assert r.status_code == 200  # degrades, never 500s
    assert r.context["report"] is None


# ── personalised dependency graph payload ──


def test_graph_payload_covers_the_whole_plan_not_just_edges(plan):
    """Nodes used to come from prerequisite edges only, so a course with no
    prerequisite row was invisible. extraNodes must carry the whole plan."""
    g = _report()["graph"]
    assert set(g["extraNodes"]) == {"TA101", "TB201", "TC301", "TCAP"}
    # TA101 and TCAP have no incoming course edge, yet must still be drawable
    edge_endpoints = {e["course_code"] for e in g["items"]} | {
        e["prerequisite_course_code"] for e in g["items"]
    }
    assert "TCAP" not in edge_endpoints  # invisible without extraNodes
    assert "TCAP" in g["extraNodes"]


def test_graph_status_matches_the_report(plan):
    r = _report()
    g = r["graph"]
    assert g["statusOf"]["TA101"] == "open"
    assert g["statusOf"]["TB201"] == "locked"
    assert g["statusOf"]["TCAP"] == "open"  # hour gate met
    # the hour-gate pseudo-prerequisite is never a graph node
    assert "100(HOURS)" not in g["statusOf"]
    assert all("HOUR" not in e["prerequisite_course_code"].upper() for e in g["items"])


def test_graph_terms_feed_the_band_axis(plan):
    g = _report()["graph"]
    assert g["termOf"]["TA101"] == 1 and g["termOf"]["TCAP"] == 9
    assert g["nameOf"]["TB201"] == "Beta"


# ── graduation tracker ──


def test_graduation_progress_uses_the_plan_not_the_registrar_total(plan):
    """The registrar's earned credits include courses outside the plan (measured:
    they disagree for most students, one has 162 earned against a 148 plan), so
    progress is per-course over the plan and the credit total is a separate fact."""
    from core.services.student_graduation import build_graduation_report

    Student.objects.filter(student_id=SID).update(total_earned_credits=999)
    g = build_graduation_report(SID, 1448, 1)
    assert g["plan_courses_total"] == 4
    assert g["plan_courses_passed"] == 0
    assert g["percent_courses"] == 0  # not distorted by the 999
    assert g["earned_credits_registrar"] == 999  # reported, never divided in
    assert g["remaining_courses"] == 4


def test_graduation_floor_is_the_prerequisite_chain(plan):
    """A -> B -> C cannot be done in fewer than 3 terms however many she takes."""
    from core.services.student_graduation import build_graduation_report

    g = build_graduation_report(SID, 1448, 1, courses_per_term=99)
    assert g["pace_terms"] == 1  # load alone would say one term
    assert g["chain_floor_terms"] == 3  # but the chain forbids it
    assert g["terms_estimate"] == 3  # the floor wins


def test_graduation_pace_wins_when_there_is_no_chain(plan):
    from core.services.student_graduation import build_graduation_report

    Prerequisite.objects.filter(program=PROG).delete()  # everything independent
    g = build_graduation_report(SID, 1448, 1, courses_per_term=2)
    assert g["chain_floor_terms"] == 1
    assert g["pace_terms"] == 2  # 4 courses at 2 a term
    assert g["terms_estimate"] == 2


def test_graduation_surfaces_the_credit_hour_gate(plan):
    from core.services.student_graduation import build_graduation_report

    Student.objects.filter(student_id=SID).update(
        total_earned_credits=40, current_registered_credits=0
    )
    g = build_graduation_report(SID, 1448, 1)
    assert [x["code"] for x in g["hour_gates"]] == ["TCAP"]
    assert g["hour_gates"][0]["remaining"] == 60


def test_studying_courses_are_not_counted_as_finished(plan):
    from core.services.student_graduation import build_graduation_report

    StudentCourse.objects.update_or_create(
        student_id=SID,
        course=Course.objects.get(course_code="TA101"),
        defaults={"status": "studying", "programme_term": 1},
    )
    g = build_graduation_report(SID, 1448, 1)
    assert g["plan_courses_passed"] == 0  # still has to pass it
    assert g["remaining_courses"] == 4
    assert len(g["in_progress"]) == 1


@override_settings(ALLOWED_HOSTS=["testserver"])
def test_graduation_screen_is_session_scoped(plan):
    u = student_otp.provision_student_user(SID)
    c = Client()
    c.force_login(u)
    r = c.get("/student/graduation/")
    assert r.status_code == 200
    assert r.context["student_id"] == SID
    assert c.get("/student/graduation/?student_id=4930002").context["student_id"] == SID


# ── student home: the timetable fallback, and the guard that makes it safe ──


@override_settings(ALLOWED_HOSTS=["testserver"])
def test_home_shows_the_published_timetable_and_names_its_term(plan):
    """The configured term has no registrations, so fall back to the one published
    timetable — but say which term it is."""
    from core.models import StudentTermSection, TermSection, TermSectionMeeting

    ts = TermSection.objects.create(course_code="TA101", course_name="Alpha", section="M1")
    TermSectionMeeting.objects.create(
        term_section=ts, day="MON", start_time="09:00", end_time="10:15", room="R1"
    )
    StudentTermSection.objects.create(
        student_id=SID, academic_year="1447", term="2", term_section=ts
    )
    u = student_otp.provision_student_user(SID)
    c = Client()
    c.force_login(u)
    r = c.get("/student/")
    assert r.context["timetable_is_fallback"] is True
    assert (r.context["timetable_year"], r.context["timetable_term"]) == ("1447", "2")
    assert r.context["timetable"], "the fallback must actually render meetings"
    ts.delete()


@override_settings(ALLOWED_HOSTS=["testserver"])
def test_no_fallback_when_two_timetables_are_loaded(plan):
    """TermSection carries no term of its own, so with two generations loaded its
    meetings could belong to either — show nothing rather than the wrong times."""
    from core.models import StudentTermSection, TermSection, TermSectionMeeting

    made = []
    for yr, tm_, sect in (("1447", "2", "M1"), ("1448", "2", "M2")):
        ts = TermSection.objects.create(course_code="TA101", course_name="Alpha", section=sect)
        TermSectionMeeting.objects.create(
            term_section=ts, day="MON", start_time="09:00", end_time="10:15", room="R1"
        )
        StudentTermSection.objects.create(
            student_id=SID, academic_year=yr, term=tm_, term_section=ts
        )
        made.append(ts)
    u = student_otp.provision_student_user(SID)
    c = Client()
    c.force_login(u)
    r = c.get("/student/")
    assert r.context["timetable_is_fallback"] is False  # ambiguous -> refuse to guess
    assert r.context["timetable"] == []
    for ts in made:
        ts.delete()

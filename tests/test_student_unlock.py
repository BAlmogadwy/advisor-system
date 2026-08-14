"""Student "what can I take / why is it locked" report + screen."""

import re

import pytest
from django.contrib.auth.models import User
from django.test import Client, override_settings
from django.urls import reverse

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


def test_failed_course_remains_open_for_retake_without_satisfying_dependants(
    plan: None,
) -> None:
    StudentCourse.objects.update_or_create(
        student_id=SID,
        course=Course.objects.get(course_code="TA101"),
        defaults={"status": "failed", "grade": "F", "mark": 55},
    )

    report = _report()

    failed = next(row for row in report["open_courses"] if row["code"] == "TA101")
    assert failed["attempt_status"] == "failed"
    assert report["counts"]["failed"] == 1
    assert "TB201" in {row["code"] for row in report["locked_courses"]}
    assert "TA101" not in {row["code"] for row in report["done"]}

    body = _render()
    assert "Retake" in body or "إعادة مقرر" in body


def test_excluding_a_studying_course_makes_it_not_taken_but_never_erases_a_pass(plan):
    course = Course.objects.get(course_code="TA101")
    StudentCourse.objects.update_or_create(
        student_id=SID,
        course=course,
        defaults={"status": "studying", "programme_term": 1},
    )

    removed = build_unlock_report(
        SID,
        1448,
        1,
        additional_studying_codes={"TA101"},
        excluded_studying_codes={"ta101"},
    )

    assert "TA101" in [row["code"] for row in removed["open_courses"]]
    assert "TA101" not in [row["code"] for row in removed["in_progress"]]
    beta = next(row for row in removed["locked_courses"] if row["code"] == "TB201")
    assert beta["reasons"][0]["code"] == "TA101"

    StudentCourse.objects.filter(student_id=SID, course=course).update(status="passed")
    completed = build_unlock_report(
        SID,
        1448,
        1,
        additional_studying_codes={"TA101"},
        excluded_studying_codes={"TA101"},
    )

    assert "TA101" in [row["code"] for row in completed["done"]]
    assert "TB201" in [row["code"] for row in completed["open_courses"]]


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
def test_course_summary_renders_disjoint_progress_states(plan):
    """The one-step count is part of locked, so peer tiles must use its remainder."""
    u = student_otp.provision_student_user(SID)
    c = Client()
    c.force_login(u)

    response = c.get("/student/courses/")
    progress = response.context["progress"]
    counts = response.context["report"]["counts"]
    assert progress["one_step"] + progress["blocked_deeper"] == counts["locked"]

    body = response.content.decode()
    for state in ("passed", "open", "one-step", "blocked-deeper"):
        assert f'data-course-state="{state}"' in body
    assert 'data-course-state="locked"' not in body


@override_settings(ALLOWED_HOSTS=["testserver"])
def test_unlock_callout_distinguishes_direct_and_chain_impact(plan):
    u = student_otp.provision_student_user(SID)
    c = Client()
    c.force_login(u)

    body = c.get(reverse("student_courses"), headers={"accept-language": "ar"}).content.decode()
    assert "أعلى أثر في سلسلة المتطلبات" in body
    assert "يفتح مباشرةً <strong>1</strong>" in body
    assert "سلسلة تضم <strong>2</strong>" in body
    assert "أفضل خطوة تالية" not in body


@override_settings(ALLOWED_HOSTS=["testserver"])
def test_recommender_candidates_are_labelled_for_next_term(plan, monkeypatch):
    real_builder = build_unlock_report

    def with_a_next_term_candidate(*args, **kwargs):
        report = real_builder(*args, **kwargs)
        report["open_courses"][0]["fits_this_term"] = True
        return report

    monkeypatch.setattr("core.student_auth_views.build_unlock_report", with_a_next_term_candidate)
    u = student_otp.provision_student_user(SID)
    c = Client()
    c.force_login(u)

    body = c.get(reverse("student_courses"), headers={"accept-language": "ar"}).content.decode()
    assert "مرشحة لخطة الفصل القادم" in body
    assert "هذا الفصل" not in body


@override_settings(ALLOWED_HOSTS=["testserver"])
def test_staff_are_redirected_off_the_student_screen(plan):
    staff = User.objects.create_user(username="adv77", password="x", is_staff=True)
    set_user_scope(staff.id, advisor_id="A1")
    c = Client()
    c.force_login(staff)
    assert c.get("/student/courses/").status_code == 302


@override_settings(ALLOWED_HOSTS=["testserver"])
def test_course_screen_links_to_a_dedicated_plan_map(plan):
    u = student_otp.provision_student_user(SID)
    c = Client()
    c.force_login(u)

    body = c.get(reverse("student_courses")).content.decode()
    assert reverse("student_plan_map") in body
    assert 'id="scGraph"' not in body
    assert "page-student-graph.js" not in body


@override_settings(ALLOWED_HOSTS=["testserver"])
def test_plan_map_uses_the_same_session_scoped_unlock_report(plan):
    u = student_otp.provision_student_user(SID)
    c = Client()
    c.force_login(u)

    r = c.get(f"{reverse('student_plan_map')}?student_id=4930002")
    assert r.status_code == 200
    assert r.context["student_id"] == SID
    assert r.context["report"]["graph"] == build_unlock_report(SID, 1448, 1)["graph"]
    body = r.content.decode()
    assert 'id="scGraph"' in body
    assert 'data-auto-render="true"' in body
    assert "page-student-graph.js" in body


@override_settings(ALLOWED_HOSTS=["testserver"])
def test_staff_are_redirected_off_the_student_plan_map(plan):
    staff = User.objects.create_user(username="map-staff", password="x", is_staff=True)
    set_user_scope(staff.id, advisor_id="A1")
    c = Client()
    c.force_login(staff)
    assert c.get(reverse("student_plan_map")).status_code == 302


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


@override_settings(ALLOWED_HOSTS=["testserver"])
def test_plan_map_survives_a_builder_failure(plan, monkeypatch):
    monkeypatch.setattr(
        "core.student_auth_views.build_unlock_report",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    u = student_otp.provision_student_user(SID)
    c = Client()
    c.force_login(u)
    r = c.get(reverse("student_plan_map"))
    assert r.status_code == 200
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


def test_graduation_lower_bound_keeps_the_prerequisite_chain(plan):
    """A -> B -> C cannot be done in fewer than 3 terms however many she takes."""
    from core.services.student_graduation import build_graduation_report

    g = build_graduation_report(SID, 1448, 1, max_credits_per_term=99)
    assert g["capacity_floor_terms_after_current"] == 1
    assert g["chain_floor_terms"] == 3  # but the chain forbids it
    assert g["lower_bound_additional_terms"] == 3


def test_graduation_credit_capacity_wins_when_there_is_no_chain(plan):
    from core.services.student_graduation import build_graduation_report

    Prerequisite.objects.filter(program=PROG).delete()  # everything independent
    g = build_graduation_report(SID, 1448, 1, max_credits_per_term=6)
    assert g["chain_floor_terms"] == 1
    assert g["capacity_floor_terms_after_current"] == 2
    assert g["lower_bound_additional_terms"] == 2


def test_stateful_recommender_rolls_passes_forward_without_writing(plan):
    from core.services.recommender import recommend_next_courses_for_state

    before = StudentCourse.objects.filter(student_id=SID).count()
    first = recommend_next_courses_for_state(
        SID, 1451, 1, passed=set(), effective_credits=100, max_credits=18
    )
    assert first == ["TA101"]

    second = recommend_next_courses_for_state(
        SID, 1452, 1, passed=set(first), effective_credits=103, max_credits=18
    )
    assert second == ["TB201"]

    third = recommend_next_courses_for_state(
        SID,
        1453,
        1,
        passed=set(first + second),
        effective_credits=106,
        max_credits=18,
    )
    assert third == ["TC301", "TCAP"]
    assert StudentCourse.objects.filter(student_id=SID).count() == before


def test_stateful_recommender_respects_simulated_credit_hour_gate(plan):
    from core.services.recommender import recommend_next_courses_for_state

    passed = {"TA101", "TB201", "TC301"}
    below = recommend_next_courses_for_state(
        SID, 1453, 1, passed=passed, effective_credits=99, max_credits=18
    )
    at_gate = recommend_next_courses_for_state(
        SID, 1453, 1, passed=passed, effective_credits=100, max_credits=18
    )
    assert "TCAP" not in below
    assert "TCAP" in at_gate


def test_graduation_uses_planner_courses_as_current_without_persisting_passes(plan):
    from core.models import StudentTermSection, TermSection
    from core.services.student_graduation import build_graduation_report

    section = TermSection.objects.create(course_code="TA101", course_name="Alpha", section="M1")
    StudentTermSection.objects.create(
        student_id=SID,
        academic_year="1448",
        term="1",
        term_section=section,
    )
    before_courses = StudentCourse.objects.filter(student_id=SID).count()
    before_sections = StudentTermSection.objects.filter(student_id=SID).count()

    g = build_graduation_report(SID, 1448, 1)

    assert [course["code"] for course in g["current_courses_assumed_passed"]] == ["TA101"]
    assert "TA101" in {course["code"] for course in g["in_progress"]}
    assert all("TA101" not in planned_term["course_codes"] for planned_term in g["term_plan"])
    assert g["registered_credits_now"] == 3
    assert StudentCourse.objects.filter(student_id=SID).count() == before_courses
    assert StudentTermSection.objects.filter(student_id=SID).count() == before_sections


def _create_aligned_graduation_transition() -> tuple[int, str, str]:
    """Create a term-7 current prerequisite and its term-8 dependant."""
    from core.models import StudentTermSection, TermSection

    student_id = 4509001  # joined in 1445; 1448/1 and 1448/2 align to levels 7 and 8
    program = "TGRP"
    prerequisite_code = "TGA701"
    dependant_code = "TGB801"
    Student.objects.create(
        student_id=student_id,
        name="Aligned Graduation Test",
        program=program,
        section="M",
        total_earned_credits=90,
        current_registered_credits=3,
    )
    for code, name, programme_term in (
        (prerequisite_code, "Current prerequisite", 7),
        (dependant_code, "Next-level dependant", 8),
    ):
        Course.objects.create(
            course_code=code,
            description=name,
            credit_hours=3,
        )
        ProgrammeRequirement.objects.create(
            program=program,
            course_code=code,
            course_name=name,
            programme_term=programme_term,
            credit_hours=3,
            type="Mandatory",
        )
    Prerequisite.objects.create(
        program=program,
        course_code=dependant_code,
        prerequisite_course_code=prerequisite_code,
    )
    section = TermSection.objects.create(
        source_tag="expected",
        course_code=prerequisite_code,
        course_key=prerequisite_code,
        section="M1",
        course_name="Current prerequisite",
    )
    StudentTermSection.objects.create(
        student_id=student_id,
        academic_year="1448",
        term="1",
        term_section=section,
        source="expected_timetable",
    )
    return student_id, prerequisite_code, dependant_code


def test_graduation_first_projection_uses_passes_from_the_planning_baseline(plan):
    from core.services.student_graduation import build_graduation_report

    student_id, prerequisite_code, dependant_code = _create_aligned_graduation_transition()

    report = build_graduation_report(student_id, 1448, 1)

    assert report["planning_baseline_academic_year"] == 1448
    assert report["planning_baseline_term"] == 1
    assert (
        report["planning_baseline_courses_assumed_passed"]
        == report["current_courses_assumed_passed"]
    )
    assert [row["code"] for row in report["current_courses_assumed_passed"]] == [prerequisite_code]
    assert report["term_plan"][0]["academic_year"] == 1448
    assert report["term_plan"][0]["term"] == 2
    assert dependant_code in report["term_plan"][0]["course_codes"]
    assert prerequisite_code not in report["term_plan"][0]["course_codes"]


def test_graduation_recommends_for_the_projected_term_not_the_baseline_term(plan, monkeypatch):
    from core.services import student_graduation

    student_id, _prerequisite_code, _dependant_code = _create_aligned_graduation_transition()
    calls: list[tuple[int, int]] = []
    real_recommender = student_graduation.recommend_next_courses_for_state

    def record_recommender(student_id, year, term, **kwargs):
        calls.append((year, term))
        return real_recommender(student_id, year, term, **kwargs)

    monkeypatch.setattr(
        student_graduation,
        "recommend_next_courses_for_state",
        record_recommender,
    )

    student_graduation.build_graduation_report(student_id, 1448, 1)

    assert calls[0] == (1448, 2)


def test_every_simulated_term_respects_the_18_credit_cap(plan):
    from core.services.student_graduation import build_graduation_report

    g = build_graduation_report(SID, 1448, 1)
    assert g["max_credits_per_term"] == 18
    assert g["term_plan"]
    assert all(planned_term["credits"] <= 18 for planned_term in g["term_plan"])


def test_incomplete_simulation_returns_a_lower_bound_and_exact_blockers(plan):
    from core.services.student_graduation import build_graduation_report

    Prerequisite.objects.filter(program=PROG, course_code="TB201").update(
        prerequisite_course_code="ZZ999"
    )
    g = build_graduation_report(SID, 1448, 1)

    assert g["simulation_completed"] is False
    assert g["estimated_additional_terms"] is None
    assert g["estimated_terms_including_current"] is None
    assert g["lower_bound_additional_terms"] >= 1
    blocked = {row["code"]: row for row in g["unresolved_requirements"]}
    assert blocked["TB201"]["missing_course_prerequisites"] == ["ZZ999"]
    assert blocked["TB201"]["missing_prerequisites_outside_plan"] == ["ZZ999"]


def _add_what_if_fixture_courses():
    Course.objects.update_or_create(
        course_code="TFILL",
        defaults={"description": "Current Plan Course", "credit_hours": 3},
    )
    ProgrammeRequirement.objects.update_or_create(
        program=PROG,
        course_code="TFILL",
        defaults={
            "course_name": "Current Plan Course",
            "programme_term": 1,
            "credit_hours": 3,
            "type": "Mandatory",
        },
    )
    Course.objects.update_or_create(
        course_code="TX999",
        defaults={"description": "Outside Prerequisite", "credit_hours": 3},
    )
    Prerequisite.objects.filter(program=PROG, course_code="TC301").update(
        prerequisite_course_code="TB201,TX999"
    )


def _map_current_courses(*codes: str):
    from core.models import StudentTermSection, TermSection

    for index, code in enumerate(codes, start=1):
        section = TermSection.objects.create(
            course_code=code,
            course_name=Course.objects.get(course_code=code).description,
            section=f"M{index}",
        )
        StudentTermSection.objects.create(
            student_id=SID,
            academic_year="1448",
            term="1",
            term_section=section,
        )


def test_current_term_replacement_rolls_into_graduation_without_database_writes(plan):
    from core.models import StudentTermSection
    from core.services.student_graduation import build_graduation_what_if

    _add_what_if_fixture_courses()
    _map_current_courses("TA101", "TFILL")
    before_courses = list(
        StudentCourse.objects.filter(student_id=SID).values_list("course__course_code", "status")
    )
    before_sections = StudentTermSection.objects.filter(student_id=SID).count()

    g = build_graduation_what_if(
        SID,
        1448,
        1,
        remove_current_courses=["TFILL"],
        add_current_courses=["TX999"],
    )
    what_if = g["what_if"]

    assert what_if["valid"] is True
    assert [row["code"] for row in what_if["removed_current_courses"]] == ["TFILL"]
    assert [row["code"] for row in what_if["added_current_courses"]] == ["TX999"]
    assert [row["code"] for row in what_if["outside_plan_additions"]] == ["TX999"]
    assert what_if["comparison"]["timing_effect"] == "FORECAST_COMPLETED"
    assert [row["code"] for row in what_if["comparison"]["blockers_resolved"]] == ["TC301"]
    assert what_if["comparison"]["deferred_courses"][0]["code"] == "TFILL"
    assert g["plan_courses_total"] == 5  # TX999 is not falsely counted as a plan course
    assert g["registered_credits_now"] == 6
    assert (
        list(
            StudentCourse.objects.filter(student_id=SID).values_list(
                "course__course_code", "status"
            )
        )
        == before_courses
    )
    assert StudentTermSection.objects.filter(student_id=SID).count() == before_sections


def test_current_term_what_if_rejects_unknown_removals_and_credit_overload(plan):
    from core.services.student_graduation import build_graduation_what_if

    _map_current_courses("TA101")
    Course.objects.update_or_create(
        course_code="TBIG",
        defaults={"description": "Too Large", "credit_hours": 19},
    )
    g = build_graduation_what_if(
        SID,
        1448,
        1,
        remove_current_courses=["NOTCURRENT"],
        add_current_courses=["TBIG"],
    )

    assert g["what_if"]["valid"] is False
    kinds = {error["kind"] for error in g["what_if"]["validation_errors"]}
    assert "NOT_IN_CURRENT_TIMETABLE" in kinds
    assert "SCENARIO_EXCEEDS_CREDIT_CAP" in kinds
    assert g["what_if"]["scenario"] is None


def test_current_term_what_if_does_not_treat_same_term_course_as_passed_prerequisite(plan):
    from core.services.student_graduation import build_graduation_what_if

    _add_what_if_fixture_courses()
    _map_current_courses("TA101", "TFILL")
    Course.objects.update_or_create(
        course_code="TADD",
        defaults={"description": "Requires Alpha first", "credit_hours": 3},
    )
    ProgrammeRequirement.objects.update_or_create(
        program=PROG,
        course_code="TADD",
        defaults={
            "course_name": "Requires Alpha first",
            "programme_term": 2,
            "credit_hours": 3,
            "type": "Mandatory",
        },
    )
    Prerequisite.objects.update_or_create(
        program=PROG,
        course_code="TADD",
        prerequisite_course_code="TA101",
    )

    result = build_graduation_what_if(
        SID,
        1448,
        1,
        remove_current_courses=["TFILL"],
        add_current_courses=["TADD"],
    )

    assert result["what_if"]["valid"] is False
    assert {
        (row.get("kind"), row.get("course_code"), tuple(row.get("missing_prerequisites") or []))
        for row in result["what_if"]["validation_errors"]
    } >= {("ADDED_COURSE_PREREQUISITES_UNMET", "TADD", ("TA101",))}


def test_replacement_search_finds_only_proven_academic_improvements(plan):
    from core.services.student_graduation import build_graduation_what_if

    _add_what_if_fixture_courses()
    _map_current_courses("TA101", "TFILL")
    g = build_graduation_what_if(
        SID,
        1448,
        1,
        search_better_replacements=True,
    )
    search = g["what_if"]

    assert search["valid"] is True
    assert "TX999" in search["candidate_courses_considered"]
    assert search["pairs_evaluated"] > 0
    assert search["search_truncated"] is False
    assert search["improving_replacements"]
    assert all(row["comparison"]["proven_improvement"] for row in search["improving_replacements"])
    assert all(
        row["comparison"]["timing_effect"] in {"EARLIER", "FORECAST_COMPLETED"}
        for row in search["improving_replacements"]
    )
    assert any(
        row["add_course"]["code"] == "TX999"
        and row["comparison"]["timing_effect"] == "FORECAST_COMPLETED"
        for row in search["improving_replacements"]
    )


def test_partial_blocker_progress_is_not_a_proven_replacement_improvement():
    from core.services.student_graduation import _compare_reports

    baseline = {
        "simulation_completed": False,
        "estimated_additional_terms": None,
        "lower_bound_additional_terms": 5,
        "registered_credits_now": 13,
        "term_plan": [],
        "unresolved_requirements": [
            {
                "code": "MATH471",
                "missing_course_prerequisites": ["MATH204"],
                "credit_hour_gate": None,
            },
            {
                "code": "DS492",
                "missing_course_prerequisites": [],
                "credit_hour_gate": {"remaining": 7},
            },
        ],
    }
    scenario = {
        "simulation_completed": False,
        "estimated_additional_terms": None,
        "lower_bound_additional_terms": 5,
        "registered_credits_now": 13,
        "term_plan": [],
        "unresolved_requirements": [
            {
                "code": "DS492",
                "missing_course_prerequisites": [],
                "credit_hour_gate": {"remaining": 4},
            }
        ],
    }

    comparison = _compare_reports(baseline, scenario, ["DS225"])

    assert comparison["timing_effect"] == "UNRESOLVED_IMPROVEMENT"
    assert comparison["blocker_progress_only"] is True
    assert comparison["proven_improvement"] is False
    assert comparison["complete_forecast_improved"] is False
    assert comparison["improvement_basis"] == "BLOCKER_PROGRESS_ONLY"


def test_earlier_complete_forecast_is_a_proven_replacement_improvement():
    from core.services.student_graduation import _compare_reports

    baseline = {
        "simulation_completed": True,
        "estimated_additional_terms": 5,
        "lower_bound_additional_terms": 5,
        "registered_credits_now": 13,
        "term_plan": [],
        "unresolved_requirements": [],
    }
    scenario = {
        "simulation_completed": True,
        "estimated_additional_terms": 4,
        "lower_bound_additional_terms": 4,
        "registered_credits_now": 13,
        "term_plan": [],
        "unresolved_requirements": [],
    }

    comparison = _compare_reports(baseline, scenario, ["DS225"])

    assert comparison["timing_effect"] == "EARLIER"
    assert comparison["terms_saved"] == 1
    assert comparison["blocker_progress_only"] is False
    assert comparison["proven_improvement"] is True
    assert comparison["complete_forecast_improved"] is True
    assert comparison["improvement_basis"] == "COMPLETE_FORECAST"


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


def _render_arabic_graduation_page():
    """Render the real student page through the session-scoped route."""
    user = student_otp.provision_student_user(SID)
    client = Client()
    client.force_login(user)
    response = client.get(
        reverse("student_graduation"),
        headers={"accept-language": "ar"},
    )
    assert response.status_code == 200
    body = response.content.decode()
    assert 'lang="ar"' in body, "Arabic assertions require the Arabic template branch"
    return response, body


@override_settings(ALLOWED_HOSTS=["testserver"])
def test_incomplete_graduation_screen_promotes_the_verified_lower_bound(plan):
    """An unresolved forecast has no estimate; its useful result is the lower bound.

    The old screen gave the prerequisite-only floor a KPI, rendered the actual
    lower bound in prose, and left the estimate as a dash. That made the weaker
    number look like the answer to "how many terms?".
    """
    Prerequisite.objects.filter(program=PROG, course_code="TB201").update(
        prerequisite_course_code="ZZ999"
    )

    response, body = _render_arabic_graduation_page()
    grad = response.context["grad"]
    assert grad["simulation_completed"] is False
    assert grad["estimated_additional_terms"] is None
    lower_bound = grad["lower_bound_additional_terms"]

    assert 'data-grad-result="lower-bound"' in body
    assert re.search(
        rf'data-grad-result="lower-bound"[\s\S]{{0,800}}>\s*{lower_bound}\s*<',
        body,
    ), "the verified lower bound must be the promoted result, not buried in prose"


@override_settings(ALLOWED_HOSTS=["testserver"])
def test_graduation_screen_names_the_scenario_and_does_not_promise_registration(plan):
    _response, body = _render_arabic_graduation_page()

    assert "محاكاة لإكمال متطلبات الخطة" in body
    assert "ليست موعدًا رسميًا للتخرج" in body
    assert "الفصل المرجعي للتخطيط" in body
    assert "الفصل الحالي" not in body
    assert "18 ساعة/فصل رئيسي" in body
    assert "ماذا أستطيع أن أسجّل الآن؟" not in body


@override_settings(ALLOWED_HOSTS=["testserver"])
def test_graduation_screen_lists_every_current_course_assumed_passed(plan):
    _map_current_courses("TA101", "TCAP")

    response, body = _render_arabic_graduation_page()
    assumed = response.context["grad"]["current_courses_assumed_passed"]
    assert {course["code"] for course in assumed} == {"TA101", "TCAP"}
    for course in assumed:
        assert course["code"] in body, (
            f"{course['code']} affects the forecast but is absent from its visible assumptions"
        )


@override_settings(ALLOWED_HOSTS=["testserver"])
def test_graduation_view_prepares_a_presentation_when_the_scenario_has_terms(plan):
    response, _body = _render_arabic_graduation_page()

    assert response.context["grad"]["term_plan"]
    assert response.context["graduation_presentation"]


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


# ── the declared type decides what is an elective slot (issue #55) ──


@pytest.fixture
def gs_plan(plan):
    """A MANDATORY course whose code starts `GS` — the shape the old rule caught.

    Seven real ones exist (Islamic Studies, Arabic Language Skills, University Life
    Skills, Computer Skills), declared `Mandatory` in 6–12 programmes each.
    """
    Course.objects.update_or_create(
        course_code="GS104", defaults={"description": "ISLAMIC VALUES", "credit_hours": 2}
    )
    ProgrammeRequirement.objects.update_or_create(
        program=PROG,
        course_code="GS104",
        defaults={"programme_term": 1, "credit_hours": 2, "type": "Mandatory"},
    )
    # A declared elective students actually TAKE. Kept, not deleted, when it
    # stopped being a placeholder: the claim "FE/GSE join the open/locked buckets
    # they belong in" needs a row to be made about.
    Course.objects.update_or_create(
        course_code="FE1", defaults={"description": "FREE ELECTIVE COURSE I", "credit_hours": 2}
    )
    ProgrammeRequirement.objects.update_or_create(
        program=PROG,
        course_code="FE1",
        defaults={"programme_term": 5, "credit_hours": 2, "type": "Free Elective"},
    )
    # And a real slot, to prove the fix did not simply disable slot detection.
    ProgrammeRequirement.objects.update_or_create(
        program=PROG,
        course_code="PE1",
        # A PROGRAM elective. `Free Elective` was the wrong choice here: students
        # take FE/GSE as ordinary courses — 111 have passed FE1 — so it is not a
        # placeholder and never was.
        defaults={"programme_term": 7, "credit_hours": 3, "type": "Program Elective"},
    )
    yield


def test_a_mandatory_course_is_not_an_elective_slot(gs_plan):
    """The defect: `GS104` is declared Mandatory and was shown as "choose one".

    Classifying by code prefix put it in `elective_slots`, which returns before the
    open/locked branch — so it appeared in neither list, carried no prerequisite
    explanation, and told the student to pick it with their adviser. There is
    nothing to pick.
    """
    r = _report()
    slots = {s["code"] for s in r["elective_slots"]}
    assert "GS104" not in slots, "a Mandatory course is being offered as a choice"
    assert "PE1" in slots, "a real elective slot must still be one"

    everywhere = (
        {c["code"] for c in r["open_courses"]}
        | {c["code"] for c in r["locked_courses"]}
        | {c["code"] for c in r["done"]}
        | {c["code"] for c in r["in_progress"]}
    )
    assert "GS104" in everywhere, "it vanished from every list a student reads"


def test_the_type_decides_regardless_of_the_code(gs_plan):
    """Both directions, so the fix cannot be 'ignore GS' rather than 'read the type'.

    And the set of types that mean PLACEHOLDER is narrower than the word "elective"
    suggests. Free and University Electives are declared electives that students
    take as ordinary courses — 111 have passed FE1, 139 GSE1 — so counting them as
    placeholders told a student who had completed GSE1 that its options were not
    yet published.
    """
    from core.services.student_helpers import is_elective_slot

    assert is_elective_slot("Program Elective") is True
    assert is_elective_slot("  programme elective ") is True
    for taken_as_a_course in ("Free Elective", "University Elective", "Mandatory", "", None):
        assert is_elective_slot(taken_as_a_course) is False, taken_as_a_course


def test_a_declared_elective_lands_in_a_bucket_a_student_reads(gs_plan):
    """`elective_slots` returns BEFORE the open/locked branch, so a code routed
    there carries no prerequisite explanation and no status at all.

    That is where `FE1` and `GSE1` went -- with 916 untaken and 364 completed
    enrolments between them -- and it is why this must be asserted through the
    report, not through the predicate. The predicate had four tests; this branch
    had none, and a mutant restoring the wide rule here survived all 2024 of them.
    """
    r = _report()
    assert "FE1" not in {s["code"] for s in r["elective_slots"]}, (
        "a course 111 students have passed is being offered as a choice"
    )
    everywhere = (
        {c["code"] for c in r["open_courses"]}
        | {c["code"] for c in r["locked_courses"]}
        | {c["code"] for c in r["done"]}
        | {c["code"] for c in r["in_progress"]}
    )
    assert "FE1" in everywhere, "it vanished from every list a student reads"
    assert "PE1" in {s["code"] for s in r["elective_slots"]}


def test_the_two_classifiers_are_one_function(gs_plan):
    """There were two, and they disagreed on seven real courses."""
    from core.services import virtual_advisor_capabilities as vac
    from core.services.student_helpers import is_elective_slot

    assert vac.is_elective_slot is is_elective_slot


def test_every_plan_row_lands_in_exactly_one_bucket(gs_plan):
    """The partition, asserted — because the misclassification moved a row between
    buckets without changing any total, which is why it went unnoticed.

    `one_step` is deliberately excluded: it is a SUBSET of `locked` (those two steps
    away), so a naive sum of `counts` double-counts and tells you nothing.
    """
    r = _report()
    c = r["counts"]
    disjoint = c["open"] + c["locked"] + c["passed"] + c["studying"] + len(r["elective_slots"])
    assert disjoint == ProgrammeRequirement.objects.filter(program=PROG).count()
    assert c["one_step"] <= c["locked"], "one_step is a subset of locked, not a sibling"


# ── what the SCREEN renders for each reason kind ──
#
# The service side was covered; the template was not. These four branches are the
# product — they are the sentences a student actually reads when told a course is
# blocked — and nothing asserted any of them.


def _render():
    """The real page, through the real URL, as the student."""
    u = student_otp.provision_student_user(SID)
    c = Client()
    c.force_login(u)
    r = c.get("/student/courses/")
    assert r.status_code == 200
    return r.content.decode()


@override_settings(ALLOWED_HOSTS=["testserver"])
def test_a_missing_course_reason_names_the_course_and_its_own_state(plan):
    body = _render()
    # TC301 is blocked by TB201, which is itself blocked by TA101.
    assert "TB201" in body
    assert "محجوب هو نفسه" in body or "it is itself blocked" in body


@override_settings(ALLOWED_HOSTS=["testserver"])
def test_a_missing_hours_reason_shows_the_numbers_not_a_label(plan):
    """`MISSING_HOURS` carries no course — its whole value is the arithmetic.

    A reason contract that flattens to `{code, text}` loses `effective/required/
    remaining`, and the student is told "you need more credit hours" without being
    told how many. TCAP is gated on 100 and the student has exactly 100, so raise
    the gate to make it bite.
    """
    Prerequisite.objects.filter(program=PROG, course_code="TCAP").update(
        prerequisite_course_code="120(HOURS)"
    )
    body = _render()
    assert "120" in body, "the requirement is missing"
    assert "100" in body, "what the student has is missing"
    assert "20" in body, "how far short they are is missing"


@override_settings(ALLOWED_HOSTS=["testserver"])
def test_an_unknown_prerequisite_is_explained_not_echoed(plan):
    """`UNKNOWN_PREREQ` fires for 74 of 320 live students.

    Its code is by definition NOT in the student's plan, so there is no name to
    show and no chain to point at. The screen must say something a person can act
    on rather than print the code alone — and must never print the kind.
    """
    Prerequisite.objects.update_or_create(
        program=PROG, course_code="TB201", prerequisite_course_code="ZZ999"
    )
    body = _render()
    assert "ZZ999" in body
    assert "غير موجود في خطتك" in body or "not found in your plan" in body
    assert "UNKNOWN_PREREQ" not in body and "unknown_prereq" not in body


@override_settings(ALLOWED_HOSTS=["testserver"])
def test_an_unrecognised_reason_kind_degrades_to_a_sentence(plan, monkeypatch):
    """The `{% else %}` branch — the one that stops a token reaching the page.

    `ASK_ADVISOR` is unreachable today only because `_build_student_plan_payload`
    emits three statuses; `StudentCourse.status` is a TextField with no choices, so
    one bad write reaches it. A future kind would land here too.
    """
    import core.services.student_unlock as su

    real = su.build_unlock_report

    def with_a_strange_reason(*a, **k):
        r = real(*a, **k)
        for c in r.get("locked_courses", []):
            c["reasons"] = [{"kind": "SOME_FUTURE_KIND"}]
        return r

    monkeypatch.setattr("core.student_auth_views.build_unlock_report", with_a_strange_reason)
    body = _render()
    assert "SOME_FUTURE_KIND" not in body and "some_future_kind" not in body
    assert "راجع مرشدك الأكاديمي" in body or "Ask your academic advisor" in body


# ── the chat path must not echo tokens either (issue #55 sibling) ──


def test_the_chat_capability_explains_an_unknown_prerequisite(plan):
    """`why()` used to fall through to `x["kind"].lower()`.

    So `unknown_prereq` went to the model, and from there to the student — for 74
    of 320 students on live data. The screen's template already handled this; the
    capability did not.
    """
    from core.services.rbac import ROLE_STUDENT
    from core.services.virtual_advisor_capabilities import _exec_my_progress

    Prerequisite.objects.update_or_create(
        program=PROG, course_code="TB201", prerequisite_course_code="ZZ999"
    )
    out = _exec_my_progress(
        {}, {"role": ROLE_STUDENT, "student_id": SID}, {"academic_year": 1448, "term": 1}
    )
    whys = [w for b in out["prerequisite_blocked"] for w in b["why"]]
    assert whys, "nothing was blocked, so this proved nothing"
    for w in whys:
        assert "_" not in w, f"an internal token reached the answer: {w!r}"
        assert w == w.lower() or " " in w, f"looks like a constant, not a sentence: {w!r}"
    assert any("not in this student's plan" in w for w in whys)


def test_every_reason_kind_becomes_a_sentence():
    """Including one that does not exist yet."""
    from core.services.virtual_advisor_capabilities import _explain_reason

    cases = [
        {"kind": "MISSING_COURSE", "code": "TA101"},
        {"kind": "MISSING_HOURS", "required": 120, "effective": 100},
        {"kind": "UNKNOWN_PREREQ", "code": "ZZ999"},
        {"kind": "ASK_ADVISOR"},
        {"kind": "SOME_FUTURE_KIND"},
        {},
    ]
    for c in cases:
        text = _explain_reason(c)
        assert text and " " in text, f"not a sentence: {text!r}"
        kind = str(c.get("kind") or "")
        # Guarded: `"" in anything` is True, so an unguarded check passes vacuously
        # for the no-kind case and asserts nothing at all.
        if kind:
            assert kind.lower() not in text, f"echoed the kind: {text!r}"


# -- the heading over `open_courses` is not a registration permission --


def test_the_open_list_does_not_tell_the_student_they_may_register(gs_plan):
    """«تستطيع تسجيلها الآن» is "you can register them now".

    PR #57 removed exactly this claim from the prerequisite badge and it survived
    as the heading of the list itself -- over a card whose own footnote correctly
    defines «متاحة» as having finished what the course requires. It matters more
    now: `FE1`/`GSE1` moved into this list, 916 untaken enrolments across 310 of
    320 students, for codes with no `TermSection` row at all.
    """
    from django.test import Client

    from core.services import student_otp

    client = Client()
    client.force_login(student_otp.provision_student_user(SID))

    # BOTH languages. The English said the same thing and the site renders `en` by
    # default, so an Arabic-only assertion would have left the live default unpinned.
    ar_body = client.get(
        reverse("student_courses"), headers={"accept-language": "ar"}
    ).content.decode()
    assert 'lang="ar"' in ar_body, (
        "the Arabic page rendered in English, so every Arabic assertion below is vacuous"
    )
    assert "تستطيع تسجيلها الآن" not in ar_body, "the registration-permission heading is back"
    assert "مقررات مستوفية المتطلبات أكاديميًا" in ar_body
    assert "هذه أهلية أكاديمية وليست إتاحة للتسجيل" in ar_body

    en_body = client.get(
        reverse("student_courses"), headers={"accept-language": "en"}
    ).content.decode()
    assert "You can take these now" not in en_body, "the English claim is still there"
    assert "Courses with requirements met" in en_body
    assert "This is academic eligibility, not registration availability" in en_body

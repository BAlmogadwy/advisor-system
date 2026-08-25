"""Strict mode on the recommendation debug screen.

The debug report used to be hard-wired to the relaxed rule (a prerequisite is
satisfied by a course still being studied; registered credits count toward hour
gates). The eligibility screen already exposed Relaxed/Strict; these tests pin
the same switch through the debug stack — batch recommender, report builder,
and the three HTTP endpoints — and pin that BOTH screens consume the one shared
rule in ``core.services.eligibility`` rather than restating it.

Fixture geometry (year 1446, semester 1):
  student 4410001 joined 1444 -> real term 4, next term 5 (odd parity)
  PASSA  term 1  passed
  STUDB  term 1  studying
  NEEDA  term 5  requires PASSA        -> recommended in both modes
  NEEDB  term 5  requires STUDB        -> relaxed only (prereq merely studying)
  HOURC  term 5  requires 30(HOURS)    -> relaxed only (earned 20 + registered
                                          15 = 35 >= 30, but earned alone < 30)
"""

import pytest
from django.contrib.auth.models import Group, User
from django.test import Client

from core.models import Course, Prerequisite, ProgrammeRequirement, Student, StudentCourse
from core.services.debug_reporting import build_recommendation_debug_report
from core.services.eligibility import effective_credits, prereq_satisfied
from core.services.rbac import ROLE_SUPER_ADMIN, ensure_role_groups
from core.services.recommender_batch import batch_recommend, batch_recommend_multi_program

pytestmark = pytest.mark.django_db

SID = 4410001
YEAR, SEM = 1446, 1
PROG = "TST"


@pytest.fixture()
def student_with_split_prereqs() -> int:
    Student.objects.create(
        student_id=SID,
        name="Test Student",
        program=PROG,
        section="M",
        total_earned_credits=20,
        current_registered_credits=15,
    )
    for code, term in [("PASSA", 1), ("STUDB", 1), ("NEEDA", 5), ("NEEDB", 5), ("HOURC", 5)]:
        ProgrammeRequirement.objects.create(
            program=PROG, course_code=code, programme_term=term, credit_hours=3
        )
    Prerequisite.objects.create(program=PROG, course_code="NEEDA", prerequisite_course_code="PASSA")
    Prerequisite.objects.create(program=PROG, course_code="NEEDB", prerequisite_course_code="STUDB")
    Prerequisite.objects.create(
        program=PROG, course_code="HOURC", prerequisite_course_code="30(HOURS)"
    )
    for code, status in [("PASSA", "passed"), ("STUDB", "studying")]:
        course = Course.objects.create(course_code=code, credit_hours=3)
        StudentCourse.objects.create(student_id=SID, course=course, status=status)
    return SID


# ── the shared rule itself ───────────────────────────────────────────────────


def test_prereq_satisfied_relaxed_accepts_studying() -> None:
    assert prereq_satisfied("X", set(), {"X"}) is True


def test_prereq_satisfied_strict_demands_a_pass() -> None:
    assert prereq_satisfied("X", set(), {"X"}, strict_passed_only=True) is False
    assert prereq_satisfied("X", {"X"}, set(), strict_passed_only=True) is True


def test_effective_credits_modes() -> None:
    assert effective_credits(20, 15) == 35
    assert effective_credits(20, 15, strict_passed_only=True) == 20
    # None-safety: the DB fields are nullable
    assert effective_credits(None, None) == 0


# ── batch recommender ────────────────────────────────────────────────────────


def test_batch_relaxed_recommends_studying_backed_courses(student_with_split_prereqs: int) -> None:
    recs = batch_recommend([SID], PROG, YEAR, SEM)[SID]
    assert "NEEDA" in recs
    assert "NEEDB" in recs, "relaxed must accept a prerequisite that is being studied"
    assert "HOURC" in recs, "relaxed must count registered credits toward the hour gate"


def test_batch_strict_drops_studying_backed_courses(student_with_split_prereqs: int) -> None:
    recs = batch_recommend([SID], PROG, YEAR, SEM, strict_passed_only=True).get(SID, [])
    assert "NEEDA" in recs, "a passed prerequisite satisfies strict mode too"
    assert "NEEDB" not in recs, "strict must not accept a merely-studying prerequisite"
    assert "HOURC" not in recs, "strict must not count registered credits toward the hour gate"


def test_batch_strict_still_excludes_courses_already_studying(
    student_with_split_prereqs: int,
) -> None:
    # Strict narrows what SATISFIES a prerequisite, not what a student may sit
    # again: a course currently being studied is never re-recommended.
    recs = batch_recommend([SID], PROG, YEAR, SEM, strict_passed_only=True).get(SID, [])
    assert "STUDB" not in recs


def test_multi_program_threads_strict_mode(student_with_split_prereqs: int) -> None:
    relaxed = batch_recommend_multi_program([SID], YEAR, SEM)[SID]
    strict = batch_recommend_multi_program([SID], YEAR, SEM, strict_passed_only=True).get(SID, [])
    assert "NEEDB" in relaxed
    assert "NEEDB" not in strict


# ── report builder ───────────────────────────────────────────────────────────


def test_report_relaxed_by_default_and_echoes_mode(student_with_split_prereqs: int) -> None:
    payload = build_recommendation_debug_report(YEAR, SEM, program=PROG)
    assert payload["filters"]["mode"] == "relaxed"
    assert "NEEDB" in payload["items"][0]["recommended_courses"]


def test_report_strict_mode(student_with_split_prereqs: int) -> None:
    payload = build_recommendation_debug_report(YEAR, SEM, program=PROG, strict_passed_only=True)
    assert payload["filters"]["mode"] == "strict"
    recs = payload["items"][0]["recommended_courses"]
    assert "NEEDA" in recs
    assert "NEEDB" not in recs


def test_report_strict_mode_through_the_multi_program_branch(
    student_with_split_prereqs: int,
) -> None:
    """A comma-separated programme filter takes the multi-programme path.

    The builder has two call sites into the batch recommender; a mode that only
    one of them threads through is a mode that silently fails for the
    "all programmes" view.
    """
    payload = build_recommendation_debug_report(
        YEAR, SEM, program=f"{PROG},ZZZ", strict_passed_only=True
    )
    recs = payload["items"][0]["recommended_courses"]
    assert "NEEDA" in recs
    assert "NEEDB" not in recs


def test_report_empty_result_still_names_the_mode() -> None:
    payload = build_recommendation_debug_report(YEAR, SEM, program="NOPE", strict_passed_only=True)
    assert payload["count"] == 0
    assert payload["filters"]["mode"] == "strict"


# ── HTTP endpoints ───────────────────────────────────────────────────────────


@pytest.fixture()
def admin_client() -> Client:
    ensure_role_groups()
    user, _ = User.objects.get_or_create(username="test-strict-admin")
    user.groups.clear()
    user.groups.add(Group.objects.get(name=ROLE_SUPER_ADMIN))
    c = Client()
    c.force_login(user)
    return c


def test_view_parses_mode_strict(admin_client: Client, student_with_split_prereqs: int) -> None:
    res = admin_client.get(
        f"/report/recommendation-debug/?year={YEAR}&semester={SEM}&program={PROG}&mode=strict"
    )
    assert res.status_code == 200
    payload = res.json()
    assert payload["filters"]["mode"] == "strict"
    assert "NEEDB" not in payload["items"][0]["recommended_courses"]


def test_view_defaults_to_relaxed(admin_client: Client, student_with_split_prereqs: int) -> None:
    res = admin_client.get(
        f"/report/recommendation-debug/?year={YEAR}&semester={SEM}&program={PROG}"
    )
    assert res.status_code == 200
    payload = res.json()
    assert payload["filters"]["mode"] == "relaxed"
    assert "NEEDB" in payload["items"][0]["recommended_courses"]


def test_view_garbage_mode_is_relaxed(
    admin_client: Client, student_with_split_prereqs: int
) -> None:
    res = admin_client.get(
        f"/report/recommendation-debug/?year={YEAR}&semester={SEM}&program={PROG}&mode=banana"
    )
    assert res.status_code == 200
    assert res.json()["filters"]["mode"] == "relaxed"


def test_csv_export_names_strict_in_filename(
    admin_client: Client, student_with_split_prereqs: int
) -> None:
    res = admin_client.get(
        f"/export/recommendation-debug.csv?year={YEAR}&semester={SEM}&program={PROG}&mode=strict"
    )
    assert res.status_code == 200
    assert "recommendation_debug_strict.csv" in res["Content-Disposition"]
    body = res.getvalue().decode("utf-8-sig")
    assert "NEEDB" not in body


def test_csv_export_relaxed_keeps_plain_filename(
    admin_client: Client, student_with_split_prereqs: int
) -> None:
    res = admin_client.get(
        f"/export/recommendation-debug.csv?year={YEAR}&semester={SEM}&program={PROG}"
    )
    assert res.status_code == 200
    assert "recommendation_debug.csv" in res["Content-Disposition"]
    assert "_strict" not in res["Content-Disposition"]


def test_xlsx_export_names_strict_in_filename(
    admin_client: Client, student_with_split_prereqs: int
) -> None:
    res = admin_client.get(
        f"/export/recommendation-debug.xlsx?year={YEAR}&semester={SEM}&program={PROG}&mode=strict"
    )
    assert res.status_code == 200
    assert f"recommendation_debug_{PROG}_{YEAR}_T{SEM}_strict.xlsx" in res["Content-Disposition"]


# ── the eligibility screen consumes the same helpers (regression guard) ──────


def test_eligibility_report_agrees_with_batch_on_the_same_student(
    student_with_split_prereqs: int,
) -> None:
    """One rule, one implementation: the two screens must answer alike.

    For NEEDB (prerequisite merely studying): eligible relaxed, blocked strict —
    exactly when the debug report does and does not recommend it.
    """
    from core.services.eligibility import build_course_eligibility_report

    relaxed = build_course_eligibility_report("NEEDB", program=PROG)
    strict = build_course_eligibility_report("NEEDB", program=PROG, strict_passed_only=True)
    relaxed_ids = {s for p in relaxed["per_program"] for s in p["eligible_student_ids"]}
    strict_ids = {s for p in strict["per_program"] for s in p["eligible_student_ids"]}
    assert SID in relaxed_ids
    assert SID not in strict_ids

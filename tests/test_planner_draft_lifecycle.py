"""The planner draft, attacked the way it will actually be attacked.

A draft arrives by reference, can be edited between being approved and being acted
on, and can be acted on twice at once. Each test below is one of those, written as
the request a client would really send rather than as a call to the service — the
rules have to hold at the door, not just in the function.
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from core.models import (
    Course,
    PlannerDraft,
    ProgrammeRequirement,
    Student,
    StudentTermSection,
    TermSection,
    TermSectionMeeting,
    TermSectionProgram,
)
from core.planner_draft_views import UNPLACED_AR, UNPLACED_AR_DEFAULT
from core.services import planner_drafts as svc
from core.services.student_planner import DraftRejected

pytestmark = pytest.mark.django_db

OWNER = 900001
STRANGER = 900002


# ── fixtures: one programme, two students, two sections per course ──


@pytest.fixture
def world():
    """A minimal but real world: a plan, a cohort, sections and meetings.

    Built rather than mocked. Every rule under test reads the database — course
    permission from the plan, cohort from the section label — so a fake would test
    the fake.
    """
    for sid in (OWNER, STRANGER):
        Student.objects.create(
            student_id=sid, name=f"Student {sid}", program="AI", section="M", status="active"
        )
    made = {}
    for code, name in (("CS113", "PROGRAMMING II"), ("AI221", "AI FUNDAMENTALS")):
        Course.objects.create(course_code=code, description=name)
        ProgrammeRequirement.objects.create(
            program="AI", course_code=code, course_name=name, credit_hours=3, type="required"
        )
        for label, day, start in (("M1", "SUN", "09:00"), ("M2", "MON", "13:00")):
            section = TermSection.objects.create(
                course_code=code, course_key=code, course_name=name, section=label
            )
            TermSectionProgram.objects.create(term_section=section, program="AI")
            TermSectionMeeting.objects.create(
                term_section=section, day=day, start_time=start, end_time="10:15"
            )
            made[(code, label)] = section
    # A section of the other cohort, to prove the planner refuses it.
    made[("CS113", "F1")] = TermSection.objects.create(
        course_code="CS113", course_key="CS113", course_name="PROGRAMMING II", section="F1"
    )
    TermSectionProgram.objects.create(term_section=made[("CS113", "F1")], program="AI")
    return made


@pytest.fixture
def draft(world):
    return svc.create_draft(student_id=OWNER, course_codes=["CS113"])


@pytest.fixture
def owner_client(client, world):
    """A session that IS the owner, provisioned the way the app provisions one.

    Through `provision_student_user`, not by writing a session key: the principal
    reads the UserScope row, so a hand-set session would prove the endpoint works
    against a fixture the real login never produces.
    """
    from core.services import student_otp

    client.force_login(student_otp.provision_student_user(OWNER))
    return client


def _url(name, draft_obj=None):
    return reverse(name, args=[str(draft_obj.id)]) if draft_obj else reverse(name)


def _post(client, name, draft_obj, payload=None):
    return client.post(
        _url(name, draft_obj), data=json.dumps(payload or {}), content_type="application/json"
    )


# ── 1. ownership ─────────────────────────────────────────────────


def test_a_draft_belonging_to_another_student_is_not_found(owner_client, world):
    """404, not 403: a 403 confirms the draft exists."""
    theirs = svc.create_draft(student_id=STRANGER, course_codes=["CS113"])
    for name in (
        "planner_draft_detail",
        "planner_draft_edit",
        "planner_draft_generate",
        "planner_draft_select",
        "planner_draft_confirm_rebuild",
    ):
        response = (
            owner_client.get(_url(name, theirs))
            if name == "planner_draft_detail"
            else _post(owner_client, name, theirs)
        )
        assert response.status_code == 404, name


def test_the_draft_id_is_never_read_from_the_payload(owner_client, world, draft):
    """A posted student_id names what the client wants, not who it is."""
    response = _post(
        owner_client, "planner_draft_edit", draft, {"student_id": STRANGER, "course_codes": []}
    )
    assert response.status_code == 200
    draft.refresh_from_db()
    assert draft.student_id == OWNER


# ── 2. what a client may name ────────────────────────────────────


def test_a_course_outside_the_students_plan_is_refused(owner_client, world, draft):
    ProgrammeRequirement.objects.create(
        program="DS", course_code="DS999", course_name="OTHER", credit_hours=3, type="required"
    )
    response = _post(owner_client, "planner_draft_edit", draft, {"course_codes": ["DS999"]})
    assert response.status_code == 400
    draft.refresh_from_db()
    assert draft.course_codes == ["CS113"], "a rejected edit must change nothing"


def test_a_section_of_the_other_cohort_is_refused(owner_client, world, draft):
    """The cohort is the section label's leading letter; M students get M sections."""
    other = world[("CS113", "F1")]
    response = _post(
        owner_client,
        "planner_draft_edit",
        draft,
        {"course_codes": ["CS113"], "fixed_sections": {"CS113": other.id}},
    )
    assert response.status_code == 400
    draft.refresh_from_db()
    assert draft.fixed_sections == {}


def test_a_pin_naming_a_section_of_a_different_course_is_refused(owner_client, world, draft):
    """`{"CS113": <an AI221 section>}` is a client asserting a relationship."""
    wrong = world[("AI221", "M1")]
    response = _post(
        owner_client,
        "planner_draft_edit",
        draft,
        {"course_codes": ["CS113"], "fixed_sections": {"CS113": wrong.id}},
    )
    assert response.status_code == 400


# ── 3. editing invalidates ───────────────────────────────────────


def test_an_edit_bumps_the_version_and_discards_the_generation(world, draft, monkeypatch):
    _stub_solver(monkeypatch, world)
    svc.generate(draft)
    draft.refresh_from_db()
    assert draft.alternatives and draft.has_current_generation

    svc.edit_draft(draft, course_codes=["CS113", "AI221"])
    draft.refresh_from_db()
    assert draft.version == 2
    assert draft.alternatives == []
    assert draft.generated_at is None
    assert draft.has_current_generation is False


def test_an_edit_discards_the_students_selection(world, draft, monkeypatch):
    """A preference is a preference about a specific set of timetables."""
    _stub_solver(monkeypatch, world)
    svc.generate(draft)
    draft.refresh_from_db()
    svc.select_alternative(draft, draft.alternatives[0]["key"])
    draft.refresh_from_db()
    assert draft.selected_alternative

    svc.edit_draft(draft, course_codes=["CS113", "AI221"])
    draft.refresh_from_db()
    assert draft.selected_alternative == ""
    assert draft.selected_at is None


def test_an_edit_that_changes_nothing_does_not_bump_the_version(world, draft):
    """Re-posting the same selection must not invalidate a live confirmation."""
    before = draft.version
    svc.edit_draft(draft, course_codes=["CS113"], keep_current_sections=True)
    draft.refresh_from_db()
    assert draft.version == before


def test_the_stale_generation_is_withheld_from_the_browser(owner_client, world, draft, monkeypatch):
    """Not shown with a caveat — withheld. Nobody reads the caveat."""
    _stub_solver(monkeypatch, world)
    svc.generate(draft)
    _post(owner_client, "planner_draft_edit", draft, {"course_codes": ["CS113", "AI221"]})

    body = owner_client.get(_url("planner_draft_detail", draft)).json()
    assert body["alternatives"] == []
    assert body["draft"]["has_current_generation"] is False


# ── 4. the rebuild confirmation ──────────────────────────────────


def test_rebuilding_without_a_confirmation_is_refused(owner_client, world, draft):
    _post(owner_client, "planner_draft_edit", draft, {"keep_current_sections": False})
    response = _post(owner_client, "planner_draft_generate", draft)
    assert response.status_code == 428
    assert response.json()["needs_confirmation"] is True


def test_a_posted_confirmed_flag_is_not_a_confirmation(owner_client, world, draft, monkeypatch):
    """The whole point: a client can fabricate both values in one request.

    Tried in BOTH states — before a token exists and while a live one does. With no
    token the check exits at the first guard and never reaches the comparison, so
    testing only that state left `compare_digest` itself unpinned: replacing it with
    `return True` passed.
    """
    _stub_solver(monkeypatch, world)
    _post(owner_client, "planner_draft_edit", draft, {"keep_current_sections": False})
    fabrications = ({"confirmed": True}, {"confirmation": "yes"}, {"confirmation": True})

    for payload in fabrications:
        assert _post(owner_client, "planner_draft_generate", draft, payload).status_code == 428, (
            f"accepted with no token outstanding: {payload}"
        )

    real = _post(owner_client, "planner_draft_confirm_rebuild", draft).json()["confirmation"]
    for payload in (*fabrications, {"confirmation": real[:-1]}, {"confirmation": real + "x"}):
        assert _post(owner_client, "planner_draft_generate", draft, payload).status_code == 428, (
            f"accepted while a live token was outstanding: {payload}"
        )
    # And the real one still works, so the test is not passing by refusing
    # everything — which is how a "nothing is accepted" assertion quietly becomes
    # true because the endpoint is broken.
    assert (
        _post(owner_client, "planner_draft_generate", draft, {"confirmation": real}).status_code
        == 200
    )


def test_the_raw_token_is_never_stored(world, draft):
    svc.edit_draft(draft, keep_current_sections=False)
    token = svc.issue_rebuild_token(draft)
    draft.refresh_from_db()
    assert token
    assert draft.rebuild_token_hash != token
    assert token not in json.dumps({"h": draft.rebuild_token_hash, "i": draft.generated_inputs}), (
        "a database reader must not be able to confirm on the student's behalf"
    )


def test_a_confirmation_is_dead_after_an_edit(world, draft):
    """Confirm a small rebuild, then quietly make it a large one."""
    svc.edit_draft(draft, keep_current_sections=False)
    token = svc.issue_rebuild_token(draft)
    svc.edit_draft(draft, course_codes=["CS113", "AI221"])
    draft.refresh_from_db()
    with pytest.raises(svc.ConfirmationRequired):
        svc.generate(draft, confirmation=token)


def test_editing_clears_the_stored_confirmation(world, draft):
    """The first of two independent guards, pinned on its own.

    Written separately from the version check below because either one alone stops
    the attack — so a single test passes with either deleted, and a test that
    passes with the code deleted is guarding nothing.
    """
    svc.edit_draft(draft, keep_current_sections=False)
    svc.issue_rebuild_token(draft)
    svc.edit_draft(draft, course_codes=["CS113", "AI221"])
    draft.refresh_from_db()
    assert draft.rebuild_token_hash == ""
    assert draft.rebuild_token_version == 0
    assert draft.rebuild_token_expires_at is None


def test_a_confirmation_left_behind_by_a_forgetful_edit_is_still_refused(world, draft):
    """The second guard: the token names a version, and a stale one does not match.

    The version is bumped WITHOUT clearing the token — which is exactly what a
    future edit path that forgot its invalidation would leave behind. The
    confirmation must still be refused on its own terms.
    """
    svc.edit_draft(draft, keep_current_sections=False)
    token = svc.issue_rebuild_token(draft)
    PlannerDraft.objects.filter(pk=draft.pk).update(version=draft.version + 1)
    draft.refresh_from_db()
    assert draft.rebuild_token_hash != "", "the setup must leave the token in place"
    with pytest.raises(svc.ConfirmationRequired):
        svc.generate(draft, confirmation=token)


def test_a_confirmation_expires(world, draft):
    svc.edit_draft(draft, keep_current_sections=False)
    token = svc.issue_rebuild_token(draft)
    PlannerDraft.objects.filter(pk=draft.pk).update(
        rebuild_token_expires_at=timezone.now() - timedelta(seconds=1)
    )
    draft.refresh_from_db()
    with pytest.raises(svc.ConfirmationRequired):
        svc.generate(draft, confirmation=token)


def test_a_confirmation_is_single_use(world, draft, monkeypatch):
    _stub_solver(monkeypatch, world)
    svc.edit_draft(draft, keep_current_sections=False)
    token = svc.issue_rebuild_token(draft)
    svc.generate(draft, confirmation=token)
    draft.refresh_from_db()
    assert draft.rebuild_token_hash == ""

    # A second rebuild of new inputs cannot reuse the spent token.
    svc.edit_draft(draft, course_codes=["CS113", "AI221"])
    draft.refresh_from_db()
    with pytest.raises(svc.ConfirmationRequired):
        svc.generate(draft, confirmation=token)


def test_another_students_confirmation_does_not_work_here(world, draft):
    """Bound to (student, draft, version) — a token is not a bearer pass."""
    theirs = svc.create_draft(student_id=STRANGER, course_codes=["CS113"])
    svc.edit_draft(theirs, keep_current_sections=False)
    stolen = svc.issue_rebuild_token(theirs)

    svc.edit_draft(draft, keep_current_sections=False)
    svc.issue_rebuild_token(draft)
    draft.refresh_from_db()
    with pytest.raises(svc.ConfirmationRequired):
        svc.generate(draft, confirmation=stolen)


def test_keeping_current_sections_needs_no_confirmation(world, draft, monkeypatch):
    """A dialog on a harmless action teaches students to click through dialogs."""
    _stub_solver(monkeypatch, world)
    assert draft.keep_current_sections is True
    svc.generate(draft)  # does not raise
    draft.refresh_from_db()
    assert draft.has_current_generation


# ── 5. generation ────────────────────────────────────────────────


def test_a_second_generation_of_one_version_reuses_the_result(world, draft, monkeypatch):
    """Two tabs must not compare one set of timetables while looking at another."""
    calls = _stub_solver(monkeypatch, world)
    first = svc.generate(draft)
    keys = [a["key"] for a in first.alternatives]
    draft.refresh_from_db()

    second = svc.generate(draft)
    assert len(calls) == 1, "the solver ran twice for one version"
    assert [a["key"] for a in second.alternatives] == keys


def test_reloading_after_a_rebuild_does_not_ask_permission_again(world, draft, monkeypatch):
    """The early return is not redundant with the claim, and this is why.

    A rebuild spends its confirmation. If the replay check did not run BEFORE the
    confirmation check, a reload — or a second tab, or the client re-fetching —
    would find the stored result unreachable behind a demand for a permission the
    student already gave and the server already consumed. They would confirm the
    same rebuild twice to see a timetable that was sitting in the row.
    """
    _stub_solver(monkeypatch, world)
    svc.edit_draft(draft, keep_current_sections=False)
    token = svc.issue_rebuild_token(draft)
    first = svc.generate(draft, confirmation=token)
    keys = [a["key"] for a in first.alternatives]

    draft.refresh_from_db()
    assert draft.rebuild_token_hash == "", "the token really was spent"
    replay = svc.generate(draft)  # no confirmation, because none should be needed
    assert [a["key"] for a in replay.alternatives] == keys


def test_two_requests_racing_past_the_early_return_still_solve_once(world, draft, monkeypatch):
    """The claim, tested where the early return cannot help.

    `has_current_generation` catches the SEQUENTIAL replay — the second request
    arrives after the first finished. It cannot catch the concurrent one, where
    both requests read "nothing generated yet" before either writes. On PostgreSQL
    the row lock serialises them; on SQLite, which is what this suite runs,
    `select_for_update` is silently a no-op, so the conditional UPDATE is the only
    thing standing between the student and two different sets of timetables.

    The interleaving is forced into the exact window: the competitor commits inside
    the confirmation check, which is the last thing that runs after `generate` has
    read "nothing generated yet" and before it claims the version. Hooking anything
    later proves nothing — the claim is deliberately taken early, so the window it
    guards is only a few lines wide.
    """
    calls = _stub_solver(monkeypatch, world)
    svc.edit_draft(draft, keep_current_sections=False)
    token = svc.issue_rebuild_token(draft)
    draft.refresh_from_db()

    real = svc._confirmation_is_valid
    fired = []

    def competitor_wins(*args, **kwargs):
        if not fired:
            fired.append(True)
            # Another request finishes this version while we are still checking.
            PlannerDraft.objects.filter(pk=draft.pk).update(
                generated_version=draft.version,
                alternatives=[{"key": "theirs", "courses": [], "meetings": []}],
                generated_inputs={"version": draft.version},
            )
        return real(*args, **kwargs)

    monkeypatch.setattr(svc, "_confirmation_is_valid", competitor_wins)
    result = svc.generate(draft, confirmation=token)

    assert fired, "the interleaving never happened, so this proved nothing"
    assert [a["key"] for a in result.alternatives] == ["theirs"], (
        "the loser overwrote the winner's timetables"
    )
    assert len(calls) == 0, "the loser ran the solver a second time for one version"


def test_a_solver_failure_rolls_back_the_token(world, draft, monkeypatch):
    """Retry must not require asking permission a second time."""
    svc.edit_draft(draft, keep_current_sections=False)
    token = svc.issue_rebuild_token(draft)

    def explode(_request):
        raise RuntimeError("solver died")

    monkeypatch.setattr(svc, "build_student_options", explode)
    with pytest.raises(RuntimeError):
        svc.generate(draft, confirmation=token)

    draft.refresh_from_db()
    assert draft.rebuild_token_hash != "", "the token was spent on a generation that never landed"
    assert draft.alternatives == []

    _stub_solver(monkeypatch, world)
    svc.generate(draft, confirmation=token)  # the same token still works
    draft.refresh_from_db()
    assert draft.has_current_generation


def test_generation_revalidates_rather_than_trusting_the_stored_draft(world, monkeypatch):
    """A section can be withdrawn between the draft being made and being used."""
    section = world[("CS113", "M1")]
    d = svc.create_draft(
        student_id=OWNER, course_codes=["CS113"], fixed_sections={"CS113": section.id}
    )
    _stub_solver(monkeypatch, world)
    section.delete()
    with pytest.raises(svc.DraftRejected):
        svc.generate(d)


def test_an_expired_draft_cannot_be_used(owner_client, world, draft):
    PlannerDraft.objects.filter(pk=draft.pk).update(
        expires_at=timezone.now() - timedelta(seconds=1)
    )
    draft.refresh_from_db()
    for name in ("planner_draft_generate", "planner_draft_edit"):
        assert _post(owner_client, name, draft, {"key": "x"}).status_code == 410, name


def test_the_term_comes_from_the_draft_not_the_request(owner_client, world, draft, monkeypatch):
    """Otherwise one version could answer for two terms, and cache the first."""
    seen = _stub_solver(monkeypatch, world)
    _post(owner_client, "planner_draft_generate", draft, {"academic_year": "1400", "term": "3"})
    assert seen[0].year == int(draft.academic_year)
    assert seen[0].term == int(draft.term)


def test_the_fingerprint_has_no_clock_in_it(world):
    """Two identical requests must fingerprint the same, or it dates rather than identifies."""

    # A real baseline row, with the field names `get_student_term_baseline` really
    # emits. A three-key literal made every one of the lookups below untestable:
    # reading `start`/`end` instead of `start_time`/`end_time` gave None for both
    # and the test still passed, which in production means two timetables differing
    # only in TIME fingerprint identically — the exact staleness this is for.
    def row(course="CS113", section="M1", day="SUN", start="09:00", end="10:15"):
        return {
            "course_code": course,
            "section": section,
            "day": day,
            "start_time": start,
            "end_time": end,
            "room": "LAB-3",
            "instructor": "د. عبدالله",
        }

    kwargs = dict(
        version=1,
        academic_year="1448",
        term="1",
        course_codes=["CS113", "AI221"],
        fixed_sections={"CS113": 4},
        keep_current_sections=True,
        baseline=[row()],
    )
    assert svc.generation_fingerprint(**kwargs) == svc.generation_fingerprint(**kwargs)

    # Order of the same material is not different material — and the two lists here
    # are genuinely different objects, unlike the version of this assertion that
    # compared a list against itself and proved nothing.
    assert svc.generation_fingerprint(**{**kwargs, "course_codes": ["AI221", "CS113"]}) == (
        svc.generation_fingerprint(**kwargs)
    )

    # Anything that changes the answer changes the fingerprint. Each baseline case
    # keeps the SAME number of rows, so nothing passes merely by counting.
    for change in (
        {"version": 2},
        {"term": "2"},
        {"academic_year": "1449"},
        {"course_codes": ["CS113", "AI221", "PHYS103"]},
        {"fixed_sections": {"CS113": 5}},
        {"keep_current_sections": False},
        {"baseline": [row(course="AI221")]},
        {"baseline": [row(section="M2")]},
        {"baseline": [row(day="MON")]},
        {"baseline": [row(start="13:00")]},
        {"baseline": [row(end="11:00")]},
        {"baseline": []},
    ):
        assert svc.generation_fingerprint(**{**kwargs, **change}) != (
            svc.generation_fingerprint(**kwargs)
        ), change


# ── 6. selection ─────────────────────────────────────────────────


def test_saving_a_timetable_is_disabled_and_writes_nothing(owner_client, world, draft, monkeypatch):
    """A V2 proposal remains on screen and cannot become stored state."""
    from core.models import StudentCourse, StudentTermSection

    _stub_solver(monkeypatch, world)
    svc.generate(draft)
    draft.refresh_from_db()
    before = (StudentCourse.objects.count(), StudentTermSection.objects.count())

    response = _post(
        owner_client, "planner_draft_select", draft, {"key": draft.alternatives[0]["key"]}
    )
    assert response.status_code == 405
    assert (StudentCourse.objects.count(), StudentTermSection.objects.count()) == before
    draft.refresh_from_db()
    assert draft.selected_alternative == ""
    assert response.json()["code"] == "TIMETABLE_SAVE_DISABLED"


def test_a_timetable_that_was_never_offered_cannot_be_saved(
    owner_client, world, draft, monkeypatch
):
    _stub_solver(monkeypatch, world)
    svc.generate(draft)
    draft.refresh_from_db()
    response = _post(owner_client, "planner_draft_select", draft, {"key": "1-2-3"})
    assert response.status_code == 405
    draft.refresh_from_db()
    assert draft.selected_alternative == ""


def test_nothing_can_be_saved_before_anything_is_generated(owner_client, world, draft):
    assert _post(owner_client, "planner_draft_select", draft, {"key": "x"}).status_code == 405


# ── 7. what reaches the browser ──────────────────────────────────


def test_the_response_carries_no_operator_metadata(owner_client, world, draft, monkeypatch):
    """Rooms, instructors, enrolment counts, the baseline and the fingerprint stay in."""
    _stub_solver(monkeypatch, world)
    _post(owner_client, "planner_draft_generate", draft)
    body = owner_client.get(_url("planner_draft_detail", draft)).content.decode()

    for leaked in ("fingerprint", "baseline", "registered_count", "instructor", "room"):
        assert leaked not in body, leaked
    payload = json.loads(body)
    meeting = payload["alternatives"][0]["meetings"][0]
    assert set(meeting) == {
        "course_code",
        "course_name",
        "section",
        "day",
        "start",
        "end",
        "source",
    }
    assert meeting["source"] in {"current", "proposed"}


def test_browser_alternative_preserves_its_own_coverage_and_unplaced_reasons(
    owner_client, world, draft, monkeypatch
):
    """A partial A2 result must not become an anonymous complete timetable."""

    svc.edit_draft(draft, course_codes=["CS113", "AI221"])

    def partial(_request):
        return {
            "generated": 3,
            "alternatives": [
                {
                    "key": "safe-key",
                    "planner_options": ["A2"],
                    "scheduled_courses": 1,
                    "target_courses": 2,
                    "course_count": 1,
                    "credit_hours": 3,
                    "days_on_campus": 1,
                    "days": ["SUN"],
                    "earliest_start": "09:00",
                    "latest_end": "10:15",
                    "courses": [
                        {
                            "course_code": "CS113",
                            "section": "M1",
                            "source": "proposed",
                        }
                    ],
                    "meetings": [
                        {
                            "course_code": "CS113",
                            "section": "M1",
                            "day": "SUN",
                            "start": "09:00",
                            "end": "10:15",
                            "source": "proposed",
                        }
                    ],
                    "unplaced": [
                        {
                            "course_code": "AI221",
                            "reason_code": "OMITTED_IN_THIS_VARIANT",
                            "reason": "internal wording must not reach the browser",
                        }
                    ],
                }
            ],
            "unplaced": [],
        }

    monkeypatch.setattr(svc, "build_student_options", partial)
    option = _post(owner_client, "planner_draft_generate", draft).json()["alternatives"][0]

    assert option["planner_options"] == ["A2"]
    assert option["scheduled_courses"] == 1
    assert option["target_courses"] == 2
    assert option["complete"] is False
    assert option["unplaced"][0]["course_code"] == "AI221"
    assert option["unplaced"][0]["reason"] == UNPLACED_AR["OMITTED_IN_THIS_VARIANT"]
    assert "internal wording" not in json.dumps(option)


def test_workspace_discloses_catalogue_scope_and_requires_complete_times(
    owner_client, world, draft
):
    TermSectionMeeting.objects.filter(term_section__course_key="AI221").delete()
    workspace = owner_client.get(_url("planner_draft_detail", draft)).json()["workspace"]
    courses = {row["course_code"]: row for row in workspace["catalog"]}

    assert workspace["section_catalog_term_known"] is False
    assert workspace["clash_check_scope"] == "recorded_complete_meeting_times"
    assert courses["AI221"]["status"] == "offering_unknown"
    assert courses["AI221"]["sections"] == []


def test_workspace_identifies_an_expected_plan_baseline(owner_client, world, draft):
    StudentTermSection.objects.create(
        student_id=OWNER,
        academic_year=draft.academic_year,
        term=draft.term,
        term_section=world[("CS113", "M1")],
        source=f"registration_plan_{draft.academic_year}_t{draft.term}",
    )

    workspace = owner_client.get(_url("planner_draft_detail", draft)).json()["workspace"]

    assert workspace["timetable_kind"] == "EXPECTED_PLAN"
    assert {row["course_code"] for row in workspace["current_timetable"]} == {"CS113"}


def test_expected_plan_proposal_uses_neutral_and_expected_fields(world, monkeypatch):
    from core.services.advisor_presentations import timetable_presentation_from_tool_results
    from core.services.llm_remote_privacy import (
        RemoteIdentityMap,
        project_tool_result_for_remote,
    )
    from core.services.rbac import ROLE_STUDENT
    from core.services.virtual_advisor_capabilities import _exec_build_timetable_proposal

    StudentTermSection.objects.create(
        student_id=OWNER,
        academic_year="1448",
        term="1",
        term_section=world[("CS113", "M1")],
        source="registration_plan_1448_t1",
    )
    monkeypatch.setattr(
        "core.services.recommender.recommend_next_courses", lambda *_args, **_kwargs: []
    )

    def no_additions(request):
        assert request.keep_current_sections is True
        assert request.must_include == ()
        return {"generated": 0, "alternatives": [], "unplaced": []}

    monkeypatch.setattr("core.services.student_planner.build_student_options", no_additions)
    result = _exec_build_timetable_proposal(
        {"mode": "around_current"},
        {"role": ROLE_STUDENT, "student_id": OWNER},
        {"academic_year": 1448, "term": 1},
    )

    assert result["ok"] is True
    assert result["baseline_kind"] == "EXPECTED_PLAN"
    assert result["baseline_sections"] == result["expected_plan_sections"]
    assert result["baseline_sections"][0]["course_code"] == "CS113"
    assert result["baseline_credit_hours"] == result["expected_plan_credit_hours"] == 3
    assert result["current_sections"] == []
    assert result["current_credit_hours"] == 0

    remote = project_tool_result_for_remote("build_timetable_proposal", result, RemoteIdentityMap())
    assert remote["baseline_kind"] == "EXPECTED_PLAN"
    assert remote["baseline_sections"] == remote["expected_plan_sections"]
    assert remote["current_sections"] == []

    presentation = timetable_presentation_from_tool_results([result])
    assert presentation["baseline_kind"] == "EXPECTED_PLAN"
    assert presentation["baseline_sections"] == presentation["expected_plan_sections"]
    assert presentation["current_sections"] == []


def test_expected_plan_legacy_builder_keeps_expected_provenance(world, monkeypatch):
    from core.services.llm_remote_privacy import (
        RemoteIdentityMap,
        project_tool_result_for_remote,
    )
    from core.services.rbac import ROLE_STUDENT
    from core.services.virtual_advisor_capabilities import _exec_build_my_timetable

    StudentTermSection.objects.create(
        student_id=OWNER,
        academic_year="1448",
        term="1",
        term_section=world[("CS113", "M1")],
        source="registration_plan_1448_t1",
    )
    monkeypatch.setattr(
        "core.services.recommender.recommend_next_courses", lambda *_args, **_kwargs: []
    )

    result = _exec_build_my_timetable(
        {},
        {"role": ROLE_STUDENT, "student_id": OWNER},
        {"academic_year": 1448, "term": 1},
    )

    assert result["ok"] is True
    assert result["baseline_kind"] == "EXPECTED_PLAN"
    assert result["retained_sections"][0]["source"] == "EXPECTED_PLAN"
    assert "CURRENT_REGISTRATION" not in str(result["retained_sections"])

    remote = project_tool_result_for_remote("build_my_timetable", result, RemoteIdentityMap())
    assert remote["baseline_kind"] == "EXPECTED_PLAN"
    assert remote["retained_sections"][0]["source"] == "EXPECTED_PLAN"


def test_coexisting_plan_and_registrar_rows_resolve_to_the_registrar_snapshot(world, monkeypatch):
    """These three tools used to fail closed on a term holding both snapshots.

    Failing closed was right while a term holding both meant something had gone
    wrong. A term now holds both by design, so the tools resolve instead: registrar
    evidence supersedes a forecast for the same term, and the plan's course must not
    appear anywhere the payload calls a registration. The refusal itself is kept and
    is still exercised by the test below -- it just cannot be reached by two
    snapshots legitimately coexisting.
    """
    from core.services.rbac import ROLE_STUDENT
    from core.services.virtual_advisor_capabilities import (
        _exec_build_timetable_proposal,
        _exec_my_clash_free_sections,
        _exec_my_timetable,
    )

    StudentTermSection.objects.create(
        student_id=OWNER,
        academic_year="1448",
        term="1",
        term_section=world[("CS113", "M1")],
        source="registration_plan_1448_t1",
    )
    StudentTermSection.objects.create(
        student_id=OWNER,
        academic_year="1448",
        term="1",
        term_section=world[("AI221", "M1")],
        source="scraper_timetable",
    )
    monkeypatch.setattr(
        "core.services.recommender.recommend_next_courses", lambda *_args, **_kwargs: []
    )

    scope = {"role": ROLE_STUDENT, "student_id": OWNER}
    ctx = {"academic_year": 1448, "term": 1}
    results = [
        _exec_my_timetable({}, scope, ctx),
        _exec_my_clash_free_sections({"course_code": "CS113"}, scope, ctx),
        _exec_build_timetable_proposal({"mode": "around_current"}, scope, ctx),
    ]

    for result in results:
        # Success is spelled differently across these three executors; what matters
        # is that none of them refused and none reported an unresolvable baseline.
        assert result.get("ok", True) is True, result
        assert result.get("reason") != "MIXED_TIMETABLE_SOURCES", result
        assert result.get("baseline_kind") in (None, "REGISTERED"), result

    timetable = results[0]
    on_screen = {row["course_code"] for row in timetable["meetings"]}
    assert on_screen == {"AI221"}, (
        "the registrar snapshot supersedes the forecast for the same term, so the "
        "planned-only course must not appear in a timetable the payload calls current"
    )
    assert timetable["schedule_kind"] == "REGISTERED"
    assert timetable["is_expected_plan"] is False


def test_a_mixed_baseline_handed_to_a_tool_is_still_refused(world, monkeypatch):
    """The fail-closed path must stay alive even though the snapshot reader can no
    longer produce a mixed set. It is the backstop for any future caller that
    assembles rows itself, which is exactly how the original defect arrived."""
    from core.services.rbac import ROLE_STUDENT
    from core.services.virtual_advisor_capabilities import _exec_my_timetable

    mixed = [
        {
            "course_code": "CS113",
            "course_key": "CS113",
            "course_name": "Planned",
            "section": "M1",
            "credits": 3,
            "day": "SUN",
            "start_time": "09:00",
            "end_time": "10:15",
            "room": "R1",
            "instructor": "Staff",
            "term_section_id": world[("CS113", "M1")].id,
            "source": "registration_plan_1448_t1",
        },
        {
            "course_code": "AI221",
            "course_key": "AI221",
            "course_name": "Registered",
            "section": "M1",
            "credits": 3,
            "day": "MON",
            "start_time": "09:00",
            "end_time": "10:15",
            "room": "R2",
            "instructor": "Staff",
            "term_section_id": world[("AI221", "M1")].id,
            "source": "scraper_timetable",
        },
    ]
    monkeypatch.setattr(
        "core.services.student_sections.get_student_term_baseline",
        lambda *_args, **_kwargs: [dict(row) for row in mixed],
    )

    result = _exec_my_timetable(
        {},
        {"role": ROLE_STUDENT, "student_id": OWNER},
        {
            "academic_year": 1448,
            "term": 1,
        },
    )

    assert result["ok"] is False
    assert result["reason"] == "MIXED_TIMETABLE_SOURCES"
    assert result["schedule_kind"] == "MIXED_REVIEW_REQUIRED"
    assert "meetings" not in result
    assert "baseline_sections" not in result


def test_a_course_the_student_never_asked_for_is_marked_as_added(
    owner_client, world, draft, monkeypatch
):
    """The builder fills the term from the plan. That must not read as the student's
    own request."""
    _stub_solver(monkeypatch, world, extra=["AI221"])
    body = _post(owner_client, "planner_draft_generate", draft).json()
    courses = {c["course_code"]: c["requested"] for c in body["alternatives"][0]["courses"]}
    assert courses == {"CS113": True, "AI221": False}


def test_no_other_students_identifier_appears(owner_client, world, draft, monkeypatch):
    _stub_solver(monkeypatch, world)
    _post(owner_client, "planner_draft_generate", draft)
    body = owner_client.get(_url("planner_draft_detail", draft)).content.decode()
    assert str(STRANGER) not in body


# ── 8. the hand-off ──────────────────────────────────────────────


def test_the_chat_handoff_returns_a_link_and_not_a_payload(owner_client, world):
    """The URL carries an id. Everything else is read back from the row."""
    response = owner_client.post(
        reverse("planner_draft_create"),
        data=json.dumps({"course_codes": ["CS113"]}),
        content_type="application/json",
    )
    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"draft_id", "url"}
    assert "CS113" not in body["url"]
    assert uuid.UUID(body["draft_id"])
    assert PlannerDraft.objects.get(id=body["draft_id"]).student_id == OWNER


def test_the_handoff_still_validates_every_code(owner_client, world):
    response = owner_client.post(
        reverse("planner_draft_create"),
        data=json.dumps({"course_codes": ["NOTMINE101"]}),
        content_type="application/json",
    )
    assert response.status_code == 400
    assert PlannerDraft.objects.count() == 0


def test_a_source_message_belonging_to_someone_else_is_ignored(owner_client, world):
    """A draft may point at the turn that produced it — one of the student's own."""
    from core.models import AdvisorConversation, AdvisorMessage

    conversation = AdvisorConversation.objects.create(student_id=STRANGER, title="theirs")
    theirs = AdvisorMessage.objects.create(
        conversation=conversation, role=AdvisorMessage.ROLE_STUDENT, content="?"
    )
    response = owner_client.post(
        reverse("planner_draft_create"),
        data=json.dumps({"course_codes": ["CS113"], "source_message_id": str(theirs.id)}),
        content_type="application/json",
    )
    assert response.status_code == 201
    assert PlannerDraft.objects.get(id=response.json()["draft_id"]).source_message_id is None


# ── 9. the page itself ───────────────────────────────────────────


def test_no_raw_template_syntax_reaches_the_student(owner_client, world, draft):
    """This screen's most reliable defect, caught mechanically rather than by eye.

    Django's `{# … #}` is SINGLE-LINE only: spread one over two lines and it is not
    parsed as a comment at all — the prose renders straight onto the page, in
    English, in the middle of an Arabic interface. It has happened on the adviser
    screen and it happened here, so it is now a test rather than a habit.
    """
    body = owner_client.get(reverse("student_planner_page", args=[str(draft.id)])).content.decode()
    for marker in ("{%", "{#", "{{"):
        assert marker not in body, f"unrendered template syntax on the page: {marker}"


def test_the_page_carries_the_draft_id_and_nothing_else_about_it(owner_client, world, draft):
    """The template renders a shell. A second serialiser here would be a second
    thing to keep in step with the endpoint, and the one that drifts is the one
    nobody tests."""
    body = owner_client.get(reverse("student_planner_page", args=[str(draft.id)])).content.decode()
    assert f'data-draft-id="{draft.id}"' in body
    assert "CS113" not in body, "the courses are fetched, not baked into the page"


def test_the_student_planner_keeps_the_student_sidebar(owner_client, world, draft):
    response = owner_client.get(reverse("student_planner_page", args=[str(draft.id)]))
    body = response.content.decode()

    assert response.context["role"] == "STUDENT"
    assert "My Academic Record" in body or "سجلي الأكاديمي" in body
    assert "Student Recommender" not in body
    assert "Program Plan Viewer" not in body
    assert "Virtual Advisor" not in body


# ── 10. anonymity ────────────────────────────────────────────────


def test_an_anonymous_request_never_reaches_the_view(client, world, draft):
    response = client.get(_url("planner_draft_detail", draft))
    assert response.status_code in (302, 301)


def test_a_signed_in_non_student_is_refused(client, world, draft):
    """`login_required` only proves somebody is signed in. The principal decides who."""
    from django.contrib.auth.models import User

    client.force_login(User.objects.create_user(username="staffer", password="x"))
    response = client.get(_url("planner_draft_detail", draft))
    assert response.status_code == 403
    assert str(OWNER) not in response.content.decode()


# ── 11. the wiring between the endpoints ─────────────────────────
#
# Everything above this line tests a rule. These test that the rules are actually
# REACHED — which is where a review found fourteen live mutants: the service was
# well covered and the wire between two views was held by nothing.


def test_the_whole_rebuild_works_over_http(owner_client, world, draft, monkeypatch):
    """The happy path, end to end, through the endpoints a browser actually calls.

    Every other confirmation test calls the service directly. Rename the response
    key at confirm-rebuild, or read a different key at generate, and all of them
    still pass while no student can ever rebuild — the one HTTP test that existed
    asserted 428, which is also what a permanently broken confirm endpoint returns.
    """
    seen = _stub_solver(monkeypatch, world)
    assert (
        _post(
            owner_client, "planner_draft_edit", draft, {"keep_current_sections": False}
        ).status_code
        == 200
    )

    issued = _post(owner_client, "planner_draft_confirm_rebuild", draft)
    assert issued.status_code == 200
    token = issued.json()["confirmation"]

    done = _post(owner_client, "planner_draft_generate", draft, {"confirmation": token})
    assert done.status_code == 200, done.content
    assert done.json()["draft"]["has_current_generation"] is True
    assert len(seen) == 1, "the solver did not run, or ran twice"


def test_the_students_two_choices_actually_reach_the_solver(
    owner_client, world, draft, monkeypatch
):
    """The retain toggle and the pins are what all the ceremony above protects.

    They were validated four ways, guarded by a one-use token, and then never
    checked to arrive: passing `keep_current_sections=True` and `fixed_sections=()`
    unconditionally left every test green. A confirmation that guards a value
    nothing reads guards nothing.
    """
    section = world[("CS113", "M2")]
    seen = _stub_solver(monkeypatch, world)
    _post(
        owner_client,
        "planner_draft_edit",
        draft,
        {
            "course_codes": ["CS113"],
            "fixed_sections": {"CS113": section.id},
            "keep_current_sections": False,
        },
    )
    token = _post(owner_client, "planner_draft_confirm_rebuild", draft).json()["confirmation"]
    _post(owner_client, "planner_draft_generate", draft, {"confirmation": token})

    assert seen[0].keep_current_sections is False, "the rebuild choice never reached the solver"
    assert dict(seen[0].fixed_sections) == {"CS113": section.id}, "the pin never reached the solver"
    assert seen[0].include_recommendations is False, (
        "the solver silently restored recommendations the student removed on screen"
    )


def test_the_student_timetable_entry_point_is_scoped_to_the_session(
    owner_client, world, monkeypatch
):
    monkeypatch.setattr(
        "core.services.recommender.recommend_next_courses", lambda *_args, **_kwargs: ["CS113"]
    )
    response = owner_client.get(reverse("student_timetable_start"))
    assert response.status_code == 302
    created = PlannerDraft.objects.latest("created_at")
    assert created.student_id == OWNER
    assert created.course_codes == ["CS113"]
    assert response.url == reverse("student_planner_page", args=[str(created.id)])


def test_the_workspace_exposes_planning_controls_but_no_save_control(owner_client, world, draft):
    detail = owner_client.get(_url("planner_draft_detail", draft)).json()
    assert detail["workspace"]["can_save_timetable"] is False
    assert detail["workspace"]["can_register_courses"] is False
    assert detail["workspace"]["catalog"]
    assert {section["label"] for section in detail["workspace"]["catalog"][0]["sections"]} <= {
        "M1",
        "M2",
    }

    page = owner_client.get(reverse("student_planner_page", args=[str(draft.id)])).content.decode()
    assert "spGenerate" in page
    assert "spCourseSearch" in page
    assert "spApply" not in page
    assert "spSave" not in page


def test_chat_timetable_tool_returns_multiple_safe_proposals(world, monkeypatch):
    from core.services.rbac import ROLE_STUDENT
    from core.services.virtual_advisor_capabilities import _exec_build_timetable_proposal

    monkeypatch.setattr(
        "core.services.recommender.recommend_next_courses", lambda *_args, **_kwargs: []
    )

    def fake_builder(request):
        assert request.keep_current_sections is False
        assert request.include_recommendations is False
        return {
            "generated": 9,
            "alternatives": [
                {
                    "planner_options": planner_options,
                    "courses": [
                        {
                            "course_code": "CS113",
                            "section": label,
                            "credits": 3,
                            "term_section_id": world[("CS113", label)].id,
                        }
                    ],
                    "meetings": [
                        {
                            "course_code": "CS113",
                            "section": label,
                            "day": day,
                            "start": start,
                            "end": "10:15",
                            "term_section_id": world[("CS113", label)].id,
                        }
                    ],
                    "credit_hours": 3,
                    "scheduled_courses": 1,
                    "target_courses": 1,
                    "unplaced": [],
                    "days_on_campus": 1,
                    "days": [day],
                    "earliest_start": start,
                    "latest_end": "10:15",
                }
                for label, day, start, planner_options in (
                    ("M1", "SUN", "09:00", ["A1", "B1"]),
                    ("M2", "MON", "13:00", ["A2"]),
                )
            ],
            "unplaced": [],
        }

    monkeypatch.setattr("core.services.student_planner.build_student_options", fake_builder)
    result = _exec_build_timetable_proposal(
        {"mode": "from_scratch", "course_codes": ["CS113"]},
        {"role": ROLE_STUDENT, "student_id": OWNER},
        {"academic_year": 1448, "term": 1},
    )

    assert result["ok"] is True
    assert len(result["alternatives"]) == 2
    assert result["alternatives_generated"] == 9
    assert result["distinct_alternatives"] == 2
    assert result["alternatives"][0]["planner_options"] == ["A1", "B1"]
    assert result["alternatives"][0]["scheduled_courses"] == 1
    assert result["alternatives"][0]["target_courses"] == 1
    assert result["can_save"] is False
    assert result["can_register"] is False
    assert "term_section_id" not in json.dumps(result)


def test_chat_timetable_tool_distinguishes_no_target_from_failed_coverage(world, monkeypatch):
    from core.services.rbac import ROLE_STUDENT
    from core.services.virtual_advisor_capabilities import _exec_build_timetable_proposal

    monkeypatch.setattr(
        "core.services.recommender.recommend_next_courses", lambda *_args, **_kwargs: []
    )

    def nothing_to_schedule(request):
        assert request.keep_current_sections is True
        assert request.must_include == ()
        return {
            "generated": 0,
            "alternatives": [],
            "unplaced": [],
            "reason": "NOTHING_TO_SCHEDULE",
        }

    monkeypatch.setattr("core.services.student_planner.build_student_options", nothing_to_schedule)
    result = _exec_build_timetable_proposal(
        {"mode": "around_current"},
        {"role": ROLE_STUDENT, "student_id": OWNER},
        {"academic_year": 1448, "term": 1},
    )

    assert result["ok"] is True
    assert result["alternatives"] == []
    assert result["no_additional_courses"] is True
    assert result["alternatives_generated"] == 0


def test_student_adapter_keeps_exact_planner_names_and_option_specific_coverage(world, monkeypatch):
    """A/B/C provenance and partial coverage must survive the chat adapter."""
    from core.services.student_planner import PlannerRequest, build_student_options

    def mapping(code, label):
        section = world[(code, label)]
        return {
            "course_code": code,
            "section": label,
            "term_section_id": section.id,
            "meetings": [
                {
                    "day": "SUN" if label == "M1" else "MON",
                    "start_time": "09:00" if label == "M1" else "13:00",
                    "end_time": "10:15",
                }
            ],
        }

    full = [mapping("CS113", "M1"), mapping("AI221", "M1")]
    partial = [mapping("CS113", "M2")]

    def fake_build_plans(**kwargs):
        assert kwargs["suggest_swaps"] is False
        assert kwargs["strict_per_course"] is False
        assert kwargs["consider_capacity"] is False
        return {
            "options": [
                {
                    "name": "A1",
                    "scheduled": 2,
                    "target": 2,
                    "mappings": full,
                    "unscheduled": [],
                },
                {
                    "name": "B1",
                    "scheduled": 2,
                    "target": 2,
                    "mappings": full,
                    "unscheduled": [],
                },
                {
                    "name": "A2",
                    "scheduled": 1,
                    "target": 2,
                    "mappings": partial,
                    "unscheduled": [{"course_code": "AI221", "reason": "ALL_SECTIONS_CLASH"}],
                },
            ]
        }

    monkeypatch.setattr("core.services.planner_builder.build_plans", fake_build_plans)
    result = build_student_options(
        PlannerRequest(
            student_id=OWNER,
            year=1448,
            term=1,
            must_include=("CS113", "AI221"),
            keep_current_sections=False,
            max_credits=18,
            include_recommendations=False,
        )
    )

    assert result["generated"] == 3
    assert len(result["alternatives"]) == 2
    assert result["alternatives"][0]["planner_options"] == ["A1", "B1"]
    assert result["alternatives"][0]["scheduled_courses"] == 2
    assert result["alternatives"][0]["target_courses"] == 2
    assert result["alternatives"][1]["planner_options"] == ["A2"]
    assert result["alternatives"][1]["scheduled_courses"] == 1
    assert result["alternatives"][1]["target_courses"] == 2
    assert result["alternatives"][1]["unplaced"][0]["course_code"] == "AI221"
    assert result["alternatives"][1]["unplaced"][0]["reason_code"] == "OMITTED_IN_THIS_VARIANT"
    assert result["unplaced"] == []


def test_the_pinned_section_is_the_one_in_the_answer(owner_client, world, draft, monkeypatch):
    """And the pin survives all the way to what the student is shown."""
    section = world[("CS113", "M2")]
    _stub_solver(monkeypatch, world)
    _post(
        owner_client,
        "planner_draft_edit",
        draft,
        {"course_codes": ["CS113"], "fixed_sections": {"CS113": section.id}},
    )
    body = _post(owner_client, "planner_draft_generate", draft).json()
    sections = {c["course_code"]: c["section"] for c in body["alternatives"][0]["courses"]}
    assert sections["CS113"] == "M2"


def test_generation_is_charged_to_the_planner_budget_not_the_advisers(
    owner_client, world, draft, monkeypatch
):
    """Which budget is spent is a behaviour, and swapping it left the suite green.

    It matters in one direction: the planner's solver measured 0.09s against a
    model turn's ninety, so sharing the adviser's allowance meant regenerating a
    timetable spent the student's questions.
    """
    from core.models import RateLimitBucket
    from core.services.rate_limit import GENERATION, PLANNING

    _stub_solver(monkeypatch, world)
    _post(owner_client, "planner_draft_generate", draft)

    spent = dict(RateLimitBucket.objects.values_list("key", "count"))
    assert spent.get(f"{PLANNING}:{OWNER}") == 1
    assert spent.get(f"{GENERATION}:{OWNER}", 0) == 0, (
        "the planner spent the adviser's question allowance"
    )


def test_a_replay_is_refunded(owner_client, world, draft, monkeypatch):
    """A result served from storage has already been paid for once."""
    from core.models import RateLimitBucket
    from core.services.rate_limit import PLANNING

    _stub_solver(monkeypatch, world)
    _post(owner_client, "planner_draft_generate", draft)
    _post(owner_client, "planner_draft_generate", draft)
    assert RateLimitBucket.objects.get(key=f"{PLANNING}:{OWNER}").count == 1


def test_asking_for_a_confirmation_costs_no_generation(owner_client, world, draft):
    """The 428 round-trip runs no solver, so it must not bill for one."""
    from core.models import RateLimitBucket
    from core.services.rate_limit import PLANNING

    _post(owner_client, "planner_draft_edit", draft, {"keep_current_sections": False})
    assert _post(owner_client, "planner_draft_generate", draft).status_code == 428
    assert not RateLimitBucket.objects.filter(key=f"{PLANNING}:{OWNER}", count__gt=0).exists()


def test_the_budget_actually_refuses(owner_client, world, draft, monkeypatch):
    """A limit nothing tests is a limit nobody knows is wired."""
    from core.services.rate_limit import LIMITS, PLANNING

    _stub_solver(monkeypatch, world)
    limit = LIMITS[PLANNING][0]
    for _ in range(limit + 2):
        # Each one edits first, so every generate is a real solve rather than a
        # refunded replay.
        svc.edit_draft(draft, course_codes=["CS113"] if _ % 2 else ["CS113", "AI221"])
        response = _post(owner_client, "planner_draft_generate", draft)
        if response.status_code == 429:
            assert int(response["Retry-After"]) > 0
            assert response.json()["retry_after"] > 0
            return
    raise AssertionError(f"{limit + 2} generations were allowed against a limit of {limit}")


def test_the_course_names_are_really_looked_up(owner_client, world, draft, monkeypatch):
    """An eleven-line docstring justifies a four-source resolver; nothing read it.

    Returning `{}` from the lookup blanked every course name on the screen and left
    the suite green.
    """
    _stub_solver(monkeypatch, world)
    body = _post(owner_client, "planner_draft_generate", draft).json()
    assert body["draft"]["requested"][0]["course_name"] == "PROGRAMMING II"
    assert body["alternatives"][0]["meetings"][0]["course_name"] == "PROGRAMMING II"


def test_no_timetable_is_marked_as_saved(owner_client, world, draft, monkeypatch):
    _stub_solver(monkeypatch, world)
    body = _post(owner_client, "planner_draft_generate", draft).json()
    key = body["alternatives"][0]["key"]
    refused = _post(owner_client, "planner_draft_select", draft, {"key": key})
    assert refused.status_code == 405
    after = owner_client.get(_url("planner_draft_detail", draft)).json()
    assert [a["selected"] for a in after["alternatives"]] == [False]


def test_the_handoff_defaults_to_the_students_own_recommendation(owner_client, world):
    """The documented PRIMARY hand-off: no course codes at all.

    Every create test posted explicit codes, so `source = None` — the branch the
    chat button actually uses — was never executed.
    """
    from unittest import mock

    with mock.patch(
        "core.services.recommender.recommend_next_courses", return_value=["CS113", "AI221"]
    ):
        response = owner_client.post(
            reverse("planner_draft_create"), data="{}", content_type="application/json"
        )
    assert response.status_code == 201
    assert PlannerDraft.objects.get(id=response.json()["draft_id"]).course_codes == [
        "CS113",
        "AI221",
    ]


def test_the_handoff_records_the_turn_it_came_from(owner_client, world):
    """The positive half. Breaking the lookup entirely left the negative test green."""
    from core.models import AdvisorConversation, AdvisorMessage

    conversation = AdvisorConversation.objects.create(student_id=OWNER, title="mine")
    mine = AdvisorMessage.objects.create(
        conversation=conversation, role=AdvisorMessage.ROLE_ASSISTANT, content="…"
    )
    response = owner_client.post(
        reverse("planner_draft_create"),
        data=json.dumps({"course_codes": ["CS113"], "source_message_id": str(mine.id)}),
        content_type="application/json",
    )
    assert response.status_code == 201
    assert PlannerDraft.objects.get(id=response.json()["draft_id"]).source_message_id == mine.id


def test_an_unparseable_source_message_id_is_not_a_crash(owner_client, world):
    """`AdvisorMessage.id` is a UUID pk, so `filter(pk="abc")` raises."""
    response = owner_client.post(
        reverse("planner_draft_create"),
        data=json.dumps({"course_codes": ["CS113"], "source_message_id": "abc"}),
        content_type="application/json",
    )
    assert response.status_code == 201
    assert PlannerDraft.objects.get(id=response.json()["draft_id"]).source_message_id is None


def test_the_handoff_honours_a_rebuild_request(owner_client, world):
    """Hardcoding this to True left the suite green."""
    response = owner_client.post(
        reverse("planner_draft_create"),
        data=json.dumps({"course_codes": ["CS113"], "keep_current_sections": False}),
        content_type="application/json",
    )
    assert PlannerDraft.objects.get(id=response.json()["draft_id"]).keep_current_sections is False


# ── 12. the rules that had two guards and therefore no test ──────


def test_a_generation_is_withheld_on_version_alone(world, draft, monkeypatch):
    """`has_current_generation` compares versions, and nothing proved it.

    An edit clears `alternatives` in the same breath as it bumps `version`, so a
    property reading only `bool(alternatives)` passed every test. The version is
    bumped here WITHOUT touching the result, which is the state a future edit path
    that forgot half its job would leave behind.
    """
    _stub_solver(monkeypatch, world)
    svc.generate(draft)
    draft.refresh_from_db()
    assert draft.has_current_generation

    PlannerDraft.objects.filter(pk=draft.pk).update(version=draft.version + 1)
    draft.refresh_from_db()
    assert draft.alternatives, "the setup must leave the stored result in place"
    assert draft.has_current_generation is False


def test_the_view_withholds_a_generation_the_version_disowns(
    owner_client, world, draft, monkeypatch
):
    """Same state, at the door. The 'withheld' test passed with the withholding
    deleted, because it arrived with nothing to withhold."""
    _stub_solver(monkeypatch, world)
    svc.generate(draft)
    PlannerDraft.objects.filter(pk=draft.pk).update(version=draft.version + 1)

    body = owner_client.get(_url("planner_draft_detail", draft)).json()
    assert body["alternatives"] == []
    assert PlannerDraft.objects.get(pk=draft.pk).alternatives, "the row still holds them"


def test_an_empty_result_is_a_result(world, draft, monkeypatch):
    """ "The solver found nothing" is an answer, not "nothing has run yet".

    Read as the latter, the retry re-ran the solver for a version that already had
    an answer, the spent confirmation made that retry demand permission again, and
    the `unplaced` reasons — the only useful output when there are no timetables —
    were computed, stored and then withheld.
    """
    calls = _stub_solver(
        monkeypatch,
        world,
        alternatives=[],
        unplaced=[{"course_code": "CS113", "reason_code": "ALL_SECTIONS_CLASH", "reason": "x"}],
    )
    svc.generate(draft)
    draft.refresh_from_db()
    assert draft.has_current_generation is True
    svc.generate(draft)
    assert len(calls) == 1, "the solver re-ran for a version that already had an answer"


def test_the_reason_a_course_did_not_fit_reaches_the_student_in_arabic(
    owner_client, world, draft, monkeypatch
):
    _stub_solver(
        monkeypatch,
        world,
        alternatives=[],
        unplaced=[
            {
                "course_code": "CS113",
                "reason_code": "ALL_SECTIONS_CLASH",
                # The solver's own English, which must NOT be what is shown.
                "reason": "No non-conflicting sections available",
            }
        ],
    )
    body = _post(owner_client, "planner_draft_generate", draft).json()
    assert len(body["unplaced"]) == 1
    shown = body["unplaced"][0]["reason"]
    assert shown == UNPLACED_AR["ALL_SECTIONS_CLASH"]
    assert "sections available" not in shown


def test_an_unrecognised_solver_reason_is_not_shown_verbatim(
    owner_client, world, draft, monkeypatch
):
    """`_translate_unplaced` falls through to the builder's internal wording."""
    _stub_solver(
        monkeypatch,
        world,
        alternatives=[],
        unplaced=[
            {
                "course_code": "CS113",
                "reason_code": "OTHER",
                "reason": "No candidate sections after hard filters",
            }
        ],
    )
    body = _post(owner_client, "planner_draft_generate", draft).json()
    assert body["unplaced"][0]["reason"] == UNPLACED_AR_DEFAULT
    assert "hard filters" not in json.dumps(body, ensure_ascii=False)


# ── 13. the ceiling, the staleness check, and the messages ───────


def test_a_suggested_timetable_respects_the_credit_ceiling(world, draft, monkeypatch):
    """`max_credits=0` means UNBOUNDED to the builder, and it was never set."""
    from core.services.credit_policy import REGULATORY_MAX_CREDITS

    seen = _stub_solver(monkeypatch, world)
    svc.generate(draft)
    assert seen[0].max_credits == REGULATORY_MAX_CREDITS
    assert seen[0].max_credits > 0, "0 is not a cap; it is the absence of one"


def test_the_summer_ceiling_is_the_lower_one(world):
    from core.services.credit_policy import REGULATORY_MAX_CREDITS, SUMMER_MAX_CREDITS_BOUND

    assert svc.credit_ceiling(1) == REGULATORY_MAX_CREDITS
    assert svc.credit_ceiling(2) == REGULATORY_MAX_CREDITS
    assert svc.credit_ceiling(3) == SUMMER_MAX_CREDITS_BOUND
    assert SUMMER_MAX_CREDITS_BOUND < REGULATORY_MAX_CREDITS


def test_a_registration_change_marks_the_generation_stale(world, draft, monkeypatch):
    """The version cannot see this. The fingerprint can, and was never compared."""
    from core.models import StudentTermSection

    _stub_solver(monkeypatch, world)
    svc.generate(draft)
    draft.refresh_from_db()
    assert svc.generation_is_stale(draft) is False

    StudentTermSection.objects.create(
        student_id=OWNER,
        term_section=world[("AI221", "M1")],
        academic_year=draft.academic_year,
        term=draft.term,
    )
    assert svc.generation_is_stale(draft) is True


def test_a_section_moving_to_a_different_hour_marks_it_stale(world, draft, monkeypatch):
    """ONLY the hour changes. Same course, same section, same day, same count.

    Adding a registration is the easy case — the list gets longer and almost any
    hash notices. Swapping one section for another is nearly as easy, because the
    label and usually the day change too. Neither proves the fingerprint reads the
    TIMES, and reading `start`/`end` instead of the `start_time`/`end_time` the
    baseline actually carries gave None for both and passed.

    It is also the realistic case: the registrar moves a section's lecture, the
    student's week changes, and the timetables built around the old hour are the
    ones that most need rebuilding.
    """
    from core.models import StudentTermSection, TermSectionMeeting

    section = world[("AI221", "M1")]
    StudentTermSection.objects.create(
        student_id=OWNER, term_section=section, academic_year="1448", term="1"
    )
    _stub_solver(monkeypatch, world)
    svc.generate(draft)
    draft.refresh_from_db()
    assert svc.generation_is_stale(draft) is False

    moved = TermSectionMeeting.objects.filter(term_section=section)
    assert moved.count() == 1
    moved.update(start_time="15:00", end_time="16:15")  # same SUN, four hours later

    assert StudentTermSection.objects.filter(student_id=OWNER).count() == 1
    assert svc.generation_is_stale(draft) is True


def test_the_browser_is_told_the_generation_is_stale(owner_client, world, draft, monkeypatch):
    """At the door, not just in the service — the screen is what says so."""
    from core.models import StudentTermSection

    _stub_solver(monkeypatch, world)
    _post(owner_client, "planner_draft_generate", draft)
    assert (
        owner_client.get(_url("planner_draft_detail", draft)).json()["draft"]["is_stale"] is False
    )

    StudentTermSection.objects.create(
        student_id=OWNER,
        term_section=world[("AI221", "M1")],
        academic_year=draft.academic_year,
        term=draft.term,
    )
    assert owner_client.get(_url("planner_draft_detail", draft)).json()["draft"]["is_stale"] is True


def test_no_english_reaches_the_student_on_any_refusal(owner_client, world, draft):
    """Every refusal below renders on an Arabic page, in place of a timetable."""
    import re

    latin = re.compile(r"[A-Za-z]{4,}")
    responses = [
        _post(owner_client, "planner_draft_edit", draft, {"course_codes": ["ZZ999"]}),
        _post(owner_client, "planner_draft_select", draft, {"key": "nope"}),
        _post(owner_client, "planner_draft_confirm_rebuild", draft),
    ]
    _post(owner_client, "planner_draft_edit", draft, {"keep_current_sections": False})
    responses.append(_post(owner_client, "planner_draft_generate", draft))

    for response in responses:
        assert response.status_code >= 400, "these are all meant to be refusals"
        message = response.json().get("error", "")
        assert message, response.content
        # A course code inside the sentence is fine; a sentence OF English is not.
        assert not latin.search(message.replace("ZZ999", "")), message


def test_the_cohort_diagnostic_never_reaches_the_student(world, monkeypatch):
    """Its own text explains a database decision to an operator."""
    from core.services import student_planner as sp
    from core.services.student_sections import UnknownStudentGender

    def refuse(_student_id):
        raise UnknownStudentGender(
            "Cannot resolve the cohort (M/F) for student 1. Refusing to query sections, "
            "because an unresolved cohort would return the other cohort's sections."
        )

    monkeypatch.setattr(sp, "student_gender_strict", refuse)
    with pytest.raises(DraftRejected) as caught:
        sp.validate_draft_selection(OWNER, ["CS113"], {})
    assert "Refusing to query" not in str(caught.value)
    assert "cohort" not in str(caught.value)


def test_a_non_string_confirmation_is_refused_not_a_crash(world, draft):
    """`_hash` would reach `True.encode`, once a live token exists."""
    svc.edit_draft(draft, keep_current_sections=False)
    svc.issue_rebuild_token(draft)
    draft.refresh_from_db()
    for bogus in (True, 123, {"a": 1}, ["x"], 1.5):
        with pytest.raises(svc.ConfirmationRequired):
            svc.generate(draft, confirmation=bogus)


def test_a_draft_with_no_term_refuses_rather_than_crashing(world, draft, monkeypatch):
    """The columns carry a blank default; `int("")` is a 500."""
    _stub_solver(monkeypatch, world)
    PlannerDraft.objects.filter(pk=draft.pk).update(academic_year="", term="")
    draft.refresh_from_db()
    with pytest.raises(svc.DraftError):
        svc.generate(draft)


def test_an_expired_draft_gets_no_fresh_confirmation(world, draft):
    """The expiry guard on the token issuer was itself unguarded."""
    svc.edit_draft(draft, keep_current_sections=False)
    PlannerDraft.objects.filter(pk=draft.pk).update(
        expires_at=timezone.now() - timedelta(seconds=1)
    )
    draft.refresh_from_db()
    with pytest.raises(svc.DraftExpired):
        svc.issue_rebuild_token(draft)


def test_a_stale_in_memory_draft_does_not_overwrite_a_newer_one(world, draft):
    """Passing an out-of-date object in is safe, because the row is re-read.

    This is the first half of the fix and the easy half. `edit_draft` takes a
    snapshot only to learn WHICH row; everything it decides from is read again
    under the lock, so tab A holding a stale object simply edits current state.
    """
    stale = PlannerDraft.objects.get(pk=draft.pk)
    svc.edit_draft(draft, course_codes=["CS113", "AI221"])

    svc.edit_draft(stale, keep_current_sections=False)
    draft.refresh_from_db()
    assert draft.version == 3, "each edit is one version, from the row and not the snapshot"
    assert draft.course_codes == ["CS113", "AI221"], "the earlier edit was not lost"
    assert draft.keep_current_sections is False


def test_a_writer_that_slips_in_mid_edit_is_refused(world, draft, monkeypatch):
    """The second half, and the one that actually needs the version guard.

    `select_for_update` is silently a no-op on SQLite — which is what the dev
    database and this whole suite run on — so two requests really can both read
    version 1 before either writes. Both then compute 2, and the loser's UPDATE
    must match zero rows rather than overwrite the winner. Without the guard the
    row ends up holding one tab's courses at the version number the OTHER tab is
    displaying, and the confirmation given on that screen validates cleanly,
    because nothing about the token is wrong.

    The interleaving is forced rather than raced: another writer commits inside the
    validation call, which is exactly the window between the read and the write.
    """
    real = svc.validate_draft_selection
    fired = []

    def slip_in(*args, **kwargs):
        if not fired:
            fired.append(True)
            PlannerDraft.objects.filter(pk=draft.pk).update(version=99)
        return real(*args, **kwargs)

    monkeypatch.setattr(svc, "validate_draft_selection", slip_in)
    with pytest.raises(svc.DraftConflict):
        svc.edit_draft(draft, course_codes=["CS113", "AI221"])

    # The losing edit wrote NOTHING — not a partial row, not a bumped version.
    #
    # The interloper's own write is rolled back with it, which a real second
    # connection's committed transaction would not be; that is the cost of forcing
    # the interleaving inside one transaction rather than racing two. What the test
    # is for survives it: without the `if not updated` guard the edit lands, no
    # exception is raised, and this fails.
    draft.refresh_from_db()
    assert draft.course_codes == ["CS113"]
    assert fired, "the interleaving never happened, so this proved nothing"


def test_an_unchanged_edit_keeps_a_live_confirmation(world, draft):
    """The claim the version test made in its docstring and never checked."""
    svc.edit_draft(draft, keep_current_sections=False)
    token = svc.issue_rebuild_token(draft)
    svc.edit_draft(draft, course_codes=["CS113"], keep_current_sections=False)
    draft.refresh_from_db()
    assert svc._confirmation_is_valid(draft, token) is True


# ── helpers ──────────────────────────────────────────────────────


def _stub_solver(monkeypatch, world, extra=(), alternatives=None, unplaced=()):
    """Replace the solver, and record what it was asked.

    The solver itself is exercised against real data elsewhere. What these tests
    are about is the lifecycle around it — how often it runs, on whose authority,
    and what survives an edit — so a deterministic stand-in makes the assertions
    about that rather than about scheduling.

    It emits the fields the REAL builder emits, including the ones the view is
    supposed to strip. A stub that emitted only the six whitelisted keys would make
    the whitelist test assert the stub's own shape: delete the whitelist and it
    still passes, because there was nothing to leak. `_meeting_rows` really does
    carry `credits`, and real sections carry rooms and instructors, so the stub
    carries them too and the test has something to catch.

    `extra` models the builder filling the term with courses the student did not
    name; `unplaced` its explanation for a course it could not place.
    """
    seen = []

    def fake(request):
        seen.append(request)
        codes = tuple(request.must_include) + tuple(extra)
        pinned = dict(request.fixed_sections)

        def section_for(code):
            """Honour a pin, so a test can prove the pin reached the solver."""
            pin = pinned.get(code)
            if pin is not None:
                return next(s for s in world.values() if s.id == pin)
            return world[(code, "M1")]

        built = alternatives
        if built is None:
            chosen = {c: section_for(c) for c in codes}
            built = [
                {
                    "key": "-".join(str(chosen[c].id) for c in codes),
                    "courses": [
                        {
                            "course_code": c,
                            "section": chosen[c].section,
                            "credits": 3,
                            "term_section_id": chosen[c].id,
                        }
                        for c in codes
                    ],
                    "meetings": [
                        {
                            "course_code": c,
                            "section": chosen[c].section,
                            "day": "SUN",
                            "start": "09:00",
                            "end": "10:15",
                            # Present in the real payload, and every one of them is
                            # something the browser must never receive.
                            "credits": 3,
                            "room": "LAB-3",
                            "instructor": "د. عبدالله",
                            "registered_count": 17,
                            "term_section_id": chosen[c].id,
                        }
                        for c in codes
                    ],
                    "course_count": len(codes),
                    "credit_hours": 3 * len(codes),
                    "days_on_campus": 1,
                    "days": ["SUN"],
                    "earliest_start": "09:00",
                    "latest_end": "10:15",
                }
            ]
        return {"alternatives": built, "unplaced": list(unplaced), "generated": len(built)}

    monkeypatch.setattr(svc, "build_student_options", fake)
    return seen


# ── 14. retention ────────────────────────────────────────────────


def test_an_abandoned_draft_goes_sooner_than_a_generated_one(world, monkeypatch):
    """Two retentions, because the two kinds of row are worth different amounts.

    An expired draft nobody generated is a course list the student walked away
    from. A generated one holds the alternatives AND the baseline the solver saw —
    their registered timetable, with instructor names, rooms and enrolment counts.
    That is the row worth keeping for a day or two and the row most worth deleting
    after that; one grace period for both gets one of them wrong.
    """
    from django.core.management import call_command

    from core.management.commands.purge_planner_drafts import (
        GENERATED_GRACE,
        UNGENERATED_GRACE,
    )

    _stub_solver(monkeypatch, world)

    def aged(days, generate=False):
        d = svc.create_draft(student_id=OWNER, course_codes=["CS113"])
        if generate:
            svc.generate(d)
        PlannerDraft.objects.filter(pk=d.pk).update(
            expires_at=timezone.now() - timedelta(days=days)
        )
        return d.pk

    live = svc.create_draft(student_id=OWNER, course_codes=["CS113"]).pk
    fresh_abandoned = aged(0)  # expired just now, never generated
    old_abandoned = aged(UNGENERATED_GRACE.days + 1)
    fresh_generated = aged(UNGENERATED_GRACE.days + 1, generate=True)
    old_generated = aged(GENERATED_GRACE.days + 1, generate=True)

    # Dry run by default, like every destructive command in this project.
    call_command("purge_planner_drafts")
    assert PlannerDraft.objects.count() == 5, "the default run deleted something"

    call_command("purge_planner_drafts", "--apply")
    survivors = set(PlannerDraft.objects.values_list("pk", flat=True))
    assert survivors == {live, fresh_abandoned, fresh_generated}, {
        "live": live in survivors,
        "fresh_abandoned": fresh_abandoned in survivors,
        "old_abandoned": old_abandoned in survivors,
        "fresh_generated": fresh_generated in survivors,
        "old_generated": old_generated in survivors,
    }


def test_a_generated_draft_is_not_swept_by_the_shorter_grace(world, monkeypatch):
    """The distinction has to be the GENERATION, not the age alone.

    Reading `generated_version > 0` without comparing it to `version` would treat a
    draft edited after generating as still generated, and keep it a week; ignoring
    the flag entirely would sweep a day-old result the student may still be reading.
    """
    from django.core.management import call_command

    from core.management.commands.purge_planner_drafts import UNGENERATED_GRACE

    _stub_solver(monkeypatch, world)
    generated = svc.create_draft(student_id=OWNER, course_codes=["CS113"])
    svc.generate(generated)
    # Edited AFTER generating: the stored result no longer describes this draft, so
    # it is abandoned rather than generated, and goes on the shorter clock.
    edited = svc.create_draft(student_id=OWNER, course_codes=["CS113"])
    svc.generate(edited)
    svc.edit_draft(edited, course_codes=["CS113", "AI221"])

    for pk in (generated.pk, edited.pk):
        PlannerDraft.objects.filter(pk=pk).update(
            expires_at=timezone.now() - timedelta(days=UNGENERATED_GRACE.days + 1)
        )

    call_command("purge_planner_drafts", "--apply")
    assert PlannerDraft.objects.filter(pk=generated.pk).exists(), "a live result was swept early"
    assert not PlannerDraft.objects.filter(pk=edited.pk).exists(), (
        "a draft whose result was superseded was kept on the long clock"
    )


def test_deleting_the_conversation_does_not_delete_the_draft(world, monkeypatch):
    """A draft outlives the turn that produced it.

    `source_message` is a provenance pointer, not an owner. If it cascaded, a
    student tidying their chat history would silently destroy a live planner draft
    — and worse, a purge run afterwards would look like the cause. The FK is
    SET_NULL for exactly this, and this is the test that says so.
    """
    from django.core.management import call_command

    from core.models import AdvisorConversation, AdvisorMessage

    conversation = AdvisorConversation.objects.create(student_id=OWNER, title="mine")
    message = AdvisorMessage.objects.create(
        conversation=conversation, role=AdvisorMessage.ROLE_ASSISTANT, content="…"
    )
    draft = svc.create_draft(student_id=OWNER, course_codes=["CS113"], source_message=message)
    assert draft.source_message_id == message.id

    conversation.delete()

    draft.refresh_from_db()
    assert draft.source_message_id is None, "the provenance link cleared, as intended"
    assert draft.is_live, "the draft itself is untouched"

    # And a purge afterwards must still judge it by age alone.
    call_command("purge_planner_drafts", "--apply")
    assert PlannerDraft.objects.filter(pk=draft.pk).exists(), (
        "a live draft was purged because its conversation was deleted"
    )


def test_a_draft_whose_student_row_vanishes_is_still_purged_by_age(world):
    """`student_id` is a bare integer, not an FK — deliberately, across the adviser.

    So a roster re-import cannot cascade a student's drafts away, and equally
    cannot leave rows the purge refuses to touch. Age is the only criterion.
    """
    from django.core.management import call_command

    from core.management.commands.purge_planner_drafts import GENERATED_GRACE

    orphan = svc.create_draft(student_id=OWNER, course_codes=["CS113"])
    PlannerDraft.objects.filter(pk=orphan.pk).update(
        expires_at=timezone.now() - GENERATED_GRACE - timedelta(days=1)
    )
    Student.objects.filter(student_id=OWNER).delete()

    call_command("purge_planner_drafts", "--apply")
    assert not PlannerDraft.objects.filter(pk=orphan.pk).exists()


def test_the_purge_agrees_with_the_application_about_what_is_generated(world, monkeypatch):
    """One predicate, two dialects — and they must not drift.

    The command's SQL mirrors `has_current_generation`: a result belongs to the
    version that produced it. Today no reachable state has `generated_version > 0`
    while disagreeing with `version`, because `edit_draft` clears it — so the
    version half of the SQL looks redundant and a mutant that drops it survives.

    That is precisely the clause worth pinning, because its job is to still be
    right when a future edit path forgets half of its invalidation. The state is
    forced here the same way the model's own test forces it.
    """
    from django.core.management import call_command

    from core.management.commands.purge_planner_drafts import UNGENERATED_GRACE

    _stub_solver(monkeypatch, world)
    draft = svc.create_draft(student_id=OWNER, course_codes=["CS113"])
    svc.generate(draft)
    draft.refresh_from_db()
    assert draft.has_current_generation

    # A forgetful writer: the version moves, the stored result does not.
    PlannerDraft.objects.filter(pk=draft.pk).update(
        version=draft.version + 1,
        expires_at=timezone.now() - timedelta(days=UNGENERATED_GRACE.days + 1),
    )
    draft.refresh_from_db()
    assert draft.has_current_generation is False, "the application calls this NOT generated"

    call_command("purge_planner_drafts", "--apply")
    assert not PlannerDraft.objects.filter(pk=draft.pk).exists(), (
        "the purge kept on the long clock a row the application treats as abandoned"
    )

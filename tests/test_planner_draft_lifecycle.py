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
    TermSection,
    TermSectionMeeting,
)
from core.services import planner_drafts as svc

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
            TermSectionMeeting.objects.create(
                term_section=section, day=day, start_time=start, end_time="10:15"
            )
            made[(code, label)] = section
    # A section of the other cohort, to prove the planner refuses it.
    made[("CS113", "F1")] = TermSection.objects.create(
        course_code="CS113", course_key="CS113", course_name="PROGRAMMING II", section="F1"
    )
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


def test_a_posted_confirmed_flag_is_not_a_confirmation(owner_client, world, draft):
    """The whole point: a client can fabricate both values in one request."""
    _post(owner_client, "planner_draft_edit", draft, {"keep_current_sections": False})
    for payload in ({"confirmed": True}, {"confirmation": "yes"}, {"confirmation": True}):
        response = _post(owner_client, "planner_draft_generate", draft, payload)
        assert response.status_code == 428, payload


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
    for name in ("planner_draft_generate", "planner_draft_edit", "planner_draft_select"):
        assert _post(owner_client, name, draft, {"key": "x"}).status_code == 410, name


def test_the_term_comes_from_the_draft_not_the_request(owner_client, world, draft, monkeypatch):
    """Otherwise one version could answer for two terms, and cache the first."""
    seen = _stub_solver(monkeypatch, world)
    _post(owner_client, "planner_draft_generate", draft, {"academic_year": "1400", "term": "3"})
    assert seen[0].year == int(draft.academic_year)
    assert seen[0].term == int(draft.term)


def test_the_fingerprint_has_no_clock_in_it(world):
    """Two identical requests must fingerprint the same, or it dates rather than identifies."""
    kwargs = dict(
        version=1,
        academic_year="1448",
        term="1",
        course_codes=["CS113"],
        fixed_sections={"CS113": 4},
        keep_current_sections=True,
        baseline=[],
    )
    assert svc.generation_fingerprint(**kwargs) == svc.generation_fingerprint(**kwargs)
    # Order of the same material is not different material.
    assert svc.generation_fingerprint(**{**kwargs, "course_codes": ["CS113"]}) == (
        svc.generation_fingerprint(**kwargs)
    )
    # Anything that changes the answer changes the fingerprint.
    for change in (
        {"version": 2},
        {"term": "2"},
        {"course_codes": ["CS113", "AI221"]},
        {"fixed_sections": {"CS113": 5}},
        {"keep_current_sections": False},
        {"baseline": [{"course_code": "X", "section": "M1", "day": "SUN"}]},
    ):
        assert svc.generation_fingerprint(**{**kwargs, **change}) != (
            svc.generation_fingerprint(**kwargs)
        ), change


# ── 6. selection ─────────────────────────────────────────────────


def test_selecting_writes_no_registration(owner_client, world, draft, monkeypatch):
    """The single most important thing this feature does NOT do."""
    from core.models import StudentCourse, StudentTermSection

    _stub_solver(monkeypatch, world)
    svc.generate(draft)
    draft.refresh_from_db()
    before = (StudentCourse.objects.count(), StudentTermSection.objects.count())

    response = _post(
        owner_client, "planner_draft_select", draft, {"key": draft.alternatives[0]["key"]}
    )
    assert response.status_code == 200
    assert (StudentCourse.objects.count(), StudentTermSection.objects.count()) == before
    assert "لم يتم تسجيلك" in response.json()["message"]


def test_a_timetable_that_was_never_offered_cannot_be_selected(
    owner_client, world, draft, monkeypatch
):
    _stub_solver(monkeypatch, world)
    svc.generate(draft)
    draft.refresh_from_db()
    response = _post(owner_client, "planner_draft_select", draft, {"key": "1-2-3"})
    assert response.status_code == 409
    draft.refresh_from_db()
    assert draft.selected_alternative == ""


def test_nothing_can_be_selected_before_anything_is_generated(owner_client, world, draft):
    assert _post(owner_client, "planner_draft_select", draft, {"key": "x"}).status_code == 409


# ── 7. what reaches the browser ──────────────────────────────────


def test_the_response_carries_no_operator_metadata(owner_client, world, draft, monkeypatch):
    """Rooms, instructors, enrolment counts, the baseline and the fingerprint stay in."""
    _stub_solver(monkeypatch, world)
    _post(owner_client, "planner_draft_generate", draft)
    body = owner_client.get(_url("planner_draft_detail", draft)).content.decode()

    for leaked in ("fingerprint", "baseline", "registered_count", "instructor", "room", "source"):
        assert leaked not in body, leaked
    payload = json.loads(body)
    meeting = payload["alternatives"][0]["meetings"][0]
    assert set(meeting) == {"course_code", "course_name", "section", "day", "start", "end"}


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


# ── helpers ──────────────────────────────────────────────────────


def _stub_solver(monkeypatch, world, extra=()):
    """Replace the solver, and record what it was asked.

    The solver itself is exercised against real data elsewhere. What these tests
    are about is the lifecycle around it — how often it runs, on whose authority,
    and what survives an edit — so a deterministic stand-in makes the assertions
    about that rather than about scheduling.

    `extra` models the real builder's own behaviour of filling the term with courses
    the student did not name.
    """
    seen = []

    def fake(request):
        seen.append(request)
        request = request.__class__(
            **{
                **{f: getattr(request, f) for f in request.__dataclass_fields__},
                "must_include": tuple(request.must_include) + tuple(extra),
            }
        )
        return {
            "alternatives": [
                {
                    "key": "-".join(str(world[(c, "M1")].id) for c in request.must_include),
                    "courses": [
                        {
                            "course_code": c,
                            "section": "M1",
                            "credits": 3,
                            "term_section_id": world[(c, "M1")].id,
                        }
                        for c in request.must_include
                    ],
                    "meetings": [
                        {
                            "course_code": c,
                            "section": "M1",
                            "day": "SUN",
                            "start": "09:00",
                            "end": "10:15",
                        }
                        for c in request.must_include
                    ],
                    "credit_hours": 3 * len(request.must_include),
                }
            ],
            "unplaced": [],
            "generated": 1,
        }

    monkeypatch.setattr(svc, "build_student_options", fake)
    return seen

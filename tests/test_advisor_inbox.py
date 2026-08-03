"""The adviser side: who may see a case, and what they may do to it.

Staff-only is not authorisation. These are mostly about the difference — a member
of staff with a login and no relationship to a student must not be able to read
that student's correspondence, and the check that stops them has to live in the
query rather than after it.
"""

from __future__ import annotations

import json

import pytest
from django.contrib.auth.models import Group, User
from django.urls import reverse

from core.models import (
    AdvisorConversation,
    AdvisorEscalation,
    AdvisorEscalationEvent,
    AdvisorMessage,
    AdvisorMessageCitation,
    FinalDisposition,
    Student,
)
from core.services.advisor_inbox import InboxError, check_transition, visible_cases
from core.services.advisor_outcome import ReasonCode
from core.services.rbac import (
    ROLE_ADVISOR,
    ROLE_GENERAL_ADVISOR,
    ROLE_SUPER_ADMIN,
    ensure_role_groups,
    set_user_scope,
)

pytestmark = pytest.mark.django_db

MINE = 9501001
THEIRS = 9501002


def _staff(username: str, role: str, *, advisor_id: str = "", departments: str = "") -> User:
    ensure_role_groups()
    user = User.objects.create_user(username, password="x")
    user.groups.add(Group.objects.get(name=role))
    set_user_scope(user.id, advisor_id=advisor_id, departments=departments)
    return user


def _case(
    student_id: int = MINE,
    *,
    program: str = "CS",
    advisor_id: str = "A1",
    status: str = AdvisorEscalation.Status.OPEN,
    question: str = "هل أقدر أنسحب؟",
) -> AdvisorEscalation:
    Student.objects.update_or_create(
        student_id=student_id,
        defaults={"name": f"S{student_id}", "program": program, "advisor_id": advisor_id},
    )
    conversation = AdvisorConversation.objects.create(student_id=student_id)
    asked = AdvisorMessage.objects.create(
        conversation=conversation, role=AdvisorMessage.ROLE_STUDENT, content=question
    )
    answered = AdvisorMessage.objects.create(
        conversation=conversation,
        role=AdvisorMessage.ROLE_ASSISTANT,
        in_reply_to=asked,
        content="لا يستطيع النظام البت في حالتك.",
        final_disposition=FinalDisposition.ABSTAIN,
        reason_codes=[ReasonCode.PROHIBITED_FOR_DECISION],
    )
    AdvisorMessageCitation.objects.create(
        message=answered,
        policy_id="TU.WITHDRAWAL.MAXIMUM",
        document_title="الدليل",
        edition="1447",
        page="24",
        authority_status="AUTHORITY_APPROVED",
        validation_status=AdvisorMessageCitation.VALID,
        source_version_hash="h",
    )
    return AdvisorEscalation.objects.create(
        conversation=conversation,
        source_message=answered,
        student_id=student_id,
        reason_code=ReasonCode.PROHIBITED_FOR_DECISION,
        status=status,
        generated_summary="ملخص",
        evidence_snapshot={
            "question": question,
            "assistant_answer": "لا يستطيع النظام البت في حالتك.",
            "answer_mode": "",
            "final_disposition": FinalDisposition.ABSTAIN,
            "reason_codes": [ReasonCode.PROHIBITED_FOR_DECISION],
            "relevant_student_facts": {},
            "citations": [],
            "missing_information": [],
        },
    )


def _act(client, case, **body):
    return client.post(
        reverse("advisor_inbox_case_action", args=[case.reference]),
        data=json.dumps(body),
        content_type="application/json",
    )


# ── who may see what ─────────────────────────────────────────────


def test_a_student_sees_no_cases_through_the_inbox(client):
    from core.services import student_otp

    case = _case()
    ensure_role_groups()
    Student.objects.get_or_create(student_id=MINE)
    client.force_login(student_otp.provision_student_user(MINE))
    assert client.get(reverse("advisor_inbox")).status_code == 403
    assert client.get(reverse("advisor_inbox_case", args=[case.reference])).status_code == 403


def test_a_student_carrying_an_adviser_id_still_sees_nothing(client):
    """The role check is not redundant, and this is the path that proves it.

    `get_user_scope` returns `advisor_id` for ANY role — it only blanks
    `student_id` for non-students. So a student whose scope row carries an adviser
    id would match cases assigned to that adviser, through the assignment ground,
    without the role check in front of it. Reached here by writing the scope
    directly, because that is what a mis-run seeding script does.
    """
    from core.services import student_otp

    case = _case(THEIRS, advisor_id="A1")
    case.assigned_adviser_id = "A1"
    case.save(update_fields=["assigned_adviser_id"])

    ensure_role_groups()
    Student.objects.get_or_create(student_id=MINE, defaults={"program": "CS"})
    student = student_otp.provision_student_user(MINE)
    set_user_scope(student.id, advisor_id="A1")

    assert list(visible_cases(student)) == []


def test_an_adviser_with_no_relationship_to_the_student_sees_nothing(client):
    """A login is not a relationship. This is the case the whole module exists for."""
    _case(advisor_id="A1")
    stranger = _staff("adv-stranger", ROLE_ADVISOR, advisor_id="A9")
    assert list(visible_cases(stranger)) == []

    client.force_login(stranger)
    page = client.get(reverse("advisor_inbox"))
    assert page.status_code == 200
    assert b"ADV-" not in page.content


def test_an_adviser_sees_cases_for_their_own_students(client):
    case = _case(advisor_id="A1")
    theirs = _staff("adv-owner", ROLE_ADVISOR, advisor_id="A1")
    assert [c.pk for c in visible_cases(theirs)] == [case.pk]


def test_an_adviser_sees_a_case_assigned_to_them_even_for_another_student(client):
    case = _case(advisor_id="A1")
    case.assigned_adviser_id = "A7"
    case.save(update_fields=["assigned_adviser_id"])
    assignee = _staff("adv-assigned", ROLE_ADVISOR, advisor_id="A7")
    assert [c.pk for c in visible_cases(assignee)] == [case.pk]


def test_a_general_adviser_sees_their_departments_and_no_others(client):
    ours = _case(MINE, program="CS", advisor_id="A1")
    _case(THEIRS, program="IS", advisor_id="A2")
    head = _staff("adv-head", ROLE_GENERAL_ADVISOR, departments="CS")
    assert [c.pk for c in visible_cases(head)] == [ours.pk]


def test_an_escalation_administrator_sees_everything(client):
    a = _case(MINE, program="CS")
    b = _case(THEIRS, program="IS")
    admin = _staff("adv-admin", ROLE_SUPER_ADMIN)
    assert {c.pk for c in visible_cases(admin)} == {a.pk, b.pk}


def test_an_adviser_with_no_id_and_no_departments_sees_nothing(client):
    """An empty scope must mean nothing, not everything."""
    _case()
    nobody = _staff("adv-empty", ROLE_ADVISOR)
    assert list(visible_cases(nobody)) == []


def test_a_case_outside_scope_is_not_found_rather_than_refused(client):
    case = _case(advisor_id="A1")
    stranger = _staff("adv-outsider", ROLE_ADVISOR, advisor_id="A9")
    client.force_login(stranger)
    assert client.get(reverse("advisor_inbox_case", args=[case.reference])).status_code == 404
    assert _act(client, case, action="assign_to_me").status_code == 404


def test_the_queue_follows_a_student_who_transfers(client):
    """Frozen evidence, LIVE routing.

    The snapshot must show what the student was given, but the queue must not keep
    a transferred student in the department that no longer advises them.
    """
    case = _case(MINE, program="CS")
    cs = _staff("adv-cs", ROLE_GENERAL_ADVISOR, departments="CS")
    ai = _staff("adv-ai", ROLE_GENERAL_ADVISOR, departments="AI")
    assert [c.pk for c in visible_cases(cs)] == [case.pk]
    assert list(visible_cases(ai)) == []

    Student.objects.filter(student_id=MINE).update(program="AI")
    assert list(visible_cases(cs)) == []
    assert [c.pk for c in visible_cases(ai)] == [case.pk]


# ── transitions ──────────────────────────────────────────────────


def test_a_case_cannot_jump_straight_to_resolved():
    """Resolving means a student was answered; there is nobody to answer yet."""
    case = _case(status=AdvisorEscalation.Status.OPEN)
    with pytest.raises(InboxError, match="cannot become"):
        check_transition(case, AdvisorEscalation.Status.RESOLVED)


def test_a_case_cannot_be_resolved_without_a_reply_to_the_student():
    case = _case(status=AdvisorEscalation.Status.ASSIGNED)
    with pytest.raises(InboxError, match="Record a reply"):
        check_transition(case, AdvisorEscalation.Status.RESOLVED)

    case.resolution_message = "تمت الموافقة."
    check_transition(case, AdvisorEscalation.Status.RESOLVED)


def test_a_closed_case_cannot_be_reopened():
    case = _case(status=AdvisorEscalation.Status.CLOSED)
    for status, _label in AdvisorEscalation.Status.choices:
        with pytest.raises(InboxError):
            check_transition(case, status)


def test_an_unknown_status_is_refused():
    with pytest.raises(InboxError, match="not a case status"):
        check_transition(_case(), "DONE_I_THINK")


def test_assigning_a_case_takes_it_out_of_the_open_pile(client):
    case = _case(advisor_id="A1")
    client.force_login(_staff("adv-1", ROLE_ADVISOR, advisor_id="A1"))
    response = _act(client, case, action="assign_to_me")

    assert response.status_code == 200
    case.refresh_from_db()
    assert case.assigned_adviser_id == "A1"
    assert case.status == AdvisorEscalation.Status.ASSIGNED


def test_an_adviser_with_no_id_cannot_assign_a_case_to_themselves(client):
    """Otherwise the case is assigned to the empty string, which is everybody."""
    case = _case(advisor_id="A1")
    admin = _staff("adv-admin2", ROLE_SUPER_ADMIN)
    client.force_login(admin)
    assert _act(client, case, action="assign_to_me").status_code == 403
    case.refresh_from_db()
    assert case.assigned_adviser_id == ""


# ── the two adviser fields stay apart ────────────────────────────


def test_notes_and_the_students_reply_are_different_fields(client):
    case = _case(advisor_id="A1")
    client.force_login(_staff("adv-2", ROLE_ADVISOR, advisor_id="A1"))

    _act(client, case, action="add_note", text="الطالب سبق أن قدّم طلبًا مشابهًا.")
    _act(client, case, action="record_response", text="تمت الموافقة، راجع العمادة.")

    case.refresh_from_db()
    assert "سبق أن قدّم" in case.adviser_notes
    assert "سبق أن قدّم" not in case.resolution_message
    assert "تمت الموافقة" in case.resolution_message
    assert "تمت الموافقة" not in case.adviser_notes


def test_notes_accumulate_rather_than_overwrite(client):
    case = _case(advisor_id="A1")
    client.force_login(_staff("adv-3", ROLE_ADVISOR, advisor_id="A1"))
    _act(client, case, action="add_note", text="أولًا")
    _act(client, case, action="add_note", text="ثانيًا")

    case.refresh_from_db()
    assert "أولًا" in case.adviser_notes and "ثانيًا" in case.adviser_notes


def test_the_student_endpoint_still_withholds_the_notes(client):
    """The separation has to hold at the serialiser, not only in the database."""
    from core.services import student_otp

    case = _case(advisor_id="A1")
    adviser = _staff("adv-4", ROLE_ADVISOR, advisor_id="A1")
    client.force_login(adviser)
    _act(client, case, action="add_note", text="ملاحظة داخلية حساسة")
    _act(client, case, action="record_response", text="ردّ للطالب")

    student_client = type(client)()
    ensure_role_groups()
    student_client.force_login(student_otp.provision_student_user(MINE))
    payload = student_client.get(reverse("advisor_escalation_detail", args=[case.reference])).json()

    # Compared against the PARSED payload: the response escapes non-ASCII, so a raw
    # substring check on the body would silently fail in both directions — never
    # finding the reply, and never finding a leaked note either.
    assert payload["escalation"]["resolution_message"] == "ردّ للطالب"
    body = json.dumps(payload, ensure_ascii=False)
    assert "ملاحظة داخلية" not in body
    assert "adviser_notes" not in body


def test_the_case_page_labels_which_box_the_student_reads(client):
    """A page where the two boxes look alike is where the mistake gets made."""
    case = _case(advisor_id="A1")
    client.force_login(_staff("adv-5", ROLE_ADVISOR, advisor_id="A1"))
    page = client.get(reverse("advisor_inbox_case", args=[case.reference])).content.decode()

    assert "the student never sees this" in page
    assert "shown in their conversation" in page


# ── the trail ────────────────────────────────────────────────────


def test_every_action_is_recorded_against_the_case(client):
    case = _case(advisor_id="A1")
    client.force_login(_staff("adv-6", ROLE_ADVISOR, advisor_id="A1"))

    client.get(reverse("advisor_inbox_case", args=[case.reference]))
    _act(client, case, action="assign_to_me")
    _act(client, case, action="add_note", text="ملاحظة")
    _act(client, case, action="record_response", text="ردّ")
    _act(client, case, action="set_status", status=AdvisorEscalation.Status.RESOLVED)

    kinds = list(case.events.values_list("kind", flat=True))
    assert AdvisorEscalationEvent.Kind.VIEWED in kinds
    assert AdvisorEscalationEvent.Kind.ASSIGNED in kinds
    assert AdvisorEscalationEvent.Kind.NOTE_ADDED in kinds
    assert AdvisorEscalationEvent.Kind.RESPONSE_RECORDED in kinds
    assert AdvisorEscalationEvent.Kind.STATUS_CHANGED in kinds
    assert all(e.actor_label for e in case.events.all())


def test_resolving_records_who_and_when(client):
    case = _case(advisor_id="A1")
    adviser = _staff("adv-7", ROLE_ADVISOR, advisor_id="A1")
    client.force_login(adviser)
    _act(client, case, action="assign_to_me")
    _act(client, case, action="record_response", text="ردّ")
    _act(client, case, action="set_status", status=AdvisorEscalation.Status.RESOLVED)

    case.refresh_from_db()
    assert case.status == AdvisorEscalation.Status.RESOLVED
    assert case.resolved_by_id == adviser.id
    assert case.resolved_at is not None


def test_an_illegal_transition_is_refused_with_its_reason(client):
    case = _case(advisor_id="A1", status=AdvisorEscalation.Status.ASSIGNED)
    client.force_login(_staff("adv-8", ROLE_ADVISOR, advisor_id="A1"))
    response = _act(client, case, action="set_status", status=AdvisorEscalation.Status.RESOLVED)

    assert response.status_code == 409
    assert "Record a reply" in response.json()["error"]
    case.refresh_from_db()
    assert case.status == AdvisorEscalation.Status.ASSIGNED


def test_an_unknown_action_changes_nothing(client):
    case = _case(advisor_id="A1")
    client.force_login(_staff("adv-9", ROLE_ADVISOR, advisor_id="A1"))
    assert _act(client, case, action="delete_everything").status_code == 400
    assert case.events.count() == 0


# ── the queue itself ─────────────────────────────────────────────


def test_the_queue_can_be_narrowed_to_what_is_mine(client):
    ours = _case(MINE, program="CS", advisor_id="A1")
    other = _case(THEIRS, program="CS", advisor_id="A1")
    ours.assigned_adviser_id = "A1"
    ours.save(update_fields=["assigned_adviser_id"])

    client.force_login(_staff("adv-10", ROLE_ADVISOR, advisor_id="A1"))
    page = client.get(reverse("advisor_inbox"), {"mine": "1"}).content.decode()
    assert ours.reference in page
    assert other.reference not in page


def test_assigned_to_me_means_nothing_when_there_is_no_me(client):
    """`assigned_adviser_id=""` matches every UNASSIGNED case.

    An administrator has no adviser id, so filtering to "mine" would hand them the
    whole unassigned queue labelled as theirs.
    """
    unassigned = _case(MINE, advisor_id="A1")
    admin = _staff("adv-noid", ROLE_SUPER_ADMIN)
    client.force_login(admin)

    page = client.get(reverse("advisor_inbox"), {"mine": "1"}).content.decode()
    assert unassigned.reference not in page
    # ...and without the filter they can still see it.
    assert unassigned.reference in client.get(reverse("advisor_inbox")).content.decode()


def test_filtering_by_status_and_reason_narrows_rather_than_widens(client):
    open_case = _case(MINE, advisor_id="A1")
    closed = _case(THEIRS, advisor_id="A1", status=AdvisorEscalation.Status.CLOSED)

    client.force_login(_staff("adv-11", ROLE_ADVISOR, advisor_id="A1"))
    page = client.get(
        reverse("advisor_inbox"), {"status": AdvisorEscalation.Status.CLOSED}
    ).content.decode()
    assert closed.reference in page
    assert open_case.reference not in page


def test_a_filter_cannot_reach_outside_the_permitted_scope(client):
    """Filters narrow a queryset that is already scoped; they never re-widen it."""
    _case(MINE, program="CS", advisor_id="A1")
    outside = _case(THEIRS, program="IS", advisor_id="A2")

    client.force_login(_staff("adv-12", ROLE_ADVISOR, advisor_id="A1"))
    page = client.get(reverse("advisor_inbox"), {"program": "IS"}).content.decode()
    assert outside.reference not in page

"""Identity fails closed, and evidence cannot carry what nobody asked for.

Both modules exist to make a class of mistake impossible rather than unlikely, so
the tests are mostly about what they REFUSE.
"""

from __future__ import annotations

import dataclasses

import pytest
from django.contrib.auth.models import User
from django.test import RequestFactory

from core.models import Student
from core.services.advisor_escalation import (
    EVIDENCE_FIELDS,
    EvidenceError,
    deterministic_summary,
    validate_evidence,
)
from core.services.advisor_principal import AdvisorPrincipal, IdentityError
from core.services.rbac import ROLE_STUDENT, ensure_role_groups, set_user_scope

pytestmark = pytest.mark.django_db

MINE = 8001001


def _request(user) -> object:
    request = RequestFactory().post("/")
    request.user = user
    return request


def _student_user(student_id: int = MINE) -> User:
    from core.services import student_otp

    ensure_role_groups()
    Student.objects.get_or_create(
        student_id=student_id,
        defaults={"name": "S", "program": "CS", "section": "M"},
    )
    return student_otp.provision_student_user(student_id)


# ── identity ─────────────────────────────────────────────────────


def test_the_principal_comes_from_the_provisioned_scope():
    principal = AdvisorPrincipal.for_student(_request(_student_user()))
    assert principal.student_id == MINE
    assert principal.role == ROLE_STUDENT


def test_the_scope_and_the_id_describe_the_same_person():
    """They cannot disagree, because one is derived from the other."""
    principal = AdvisorPrincipal.for_student(_request(_student_user()))
    assert principal.as_scope() == {"role": ROLE_STUDENT, "student_id": principal.student_id}


def test_a_principal_cannot_be_edited_after_it_is_built():
    """Identity is not something a handler adjusts as it goes."""
    principal = AdvisorPrincipal.for_student(_request(_student_user()))
    with pytest.raises(dataclasses.FrozenInstanceError):
        principal.student_id = 9999999  # type: ignore[misc]


def test_a_payload_student_id_cannot_become_the_principal():
    """A request that names a student id is describing what it wants, not who it is."""
    request = RequestFactory().post("/", data={"student_id": 7777777})
    request.user = _student_user()
    assert AdvisorPrincipal.for_student(request).student_id == MINE


def test_a_non_student_is_refused_rather_than_downgraded():
    ensure_role_groups()
    staff = User.objects.create_user("someadviser", password="x")
    with pytest.raises(IdentityError):
        AdvisorPrincipal.for_student(_request(staff))


def test_a_non_student_carrying_a_student_id_is_still_refused():
    """The role check is not redundant, it is the second lock.

    `get_user_scope` already blanks `student_id` for non-students, so removing the
    role check here changes nothing today — which is exactly why it would be
    deleted as dead code. Patched to return the scope that helper promises never to
    produce, so this pins the principal's OWN contract rather than the helper's.
    """
    from unittest import mock

    with mock.patch(
        "core.services.advisor_principal.get_user_scope",
        return_value={"role": "GENERAL_ADVISOR", "student_id": MINE},
    ):
        with pytest.raises(IdentityError, match="signed-in students"):
            AdvisorPrincipal.for_student(_request(_student_user()))


def test_staff_scope_never_carries_a_student_identity():
    """A subject is not an identity.

    Staff are authorised for a student by `require_student_scope`; putting that
    student into the scope dict would make the capability layer treat the adviser
    as if they WERE the student, and the student clamp would silently start
    applying to a staff session.
    """
    ensure_role_groups()
    from django.contrib.auth.models import Group

    from core.services.rbac import ROLE_GENERAL_ADVISOR

    staff = User.objects.create_user("adviser-one", password="x")
    staff.groups.add(Group.objects.get(name=ROLE_GENERAL_ADVISOR))
    principal = AdvisorPrincipal.for_staff(_request(staff), subject_student_id=MINE)

    assert principal.student_id == MINE, "the subject record still loads"
    assert principal.as_scope()["student_id"] is None, "but it is not their identity"
    assert principal.as_scope()["role"] == ROLE_GENERAL_ADVISOR


def test_a_student_cannot_enter_through_the_staff_constructor():
    with pytest.raises(IdentityError, match="student adviser"):
        AdvisorPrincipal.for_staff(_request(_student_user()), subject_student_id=999)


def test_a_negative_student_id_is_refused_rather_than_clamped():
    """The WhatsApp gateway signals "no student" with -1.

    A clamp to a minimum of 1 turned that denial into student number 1 — a refusal
    that resolves to a real person's record.
    """
    ensure_role_groups()
    from django.contrib.auth.models import Group

    from core.services.rbac import ROLE_GENERAL_ADVISOR

    staff = User.objects.create_user("adviser-two", password="x")
    staff.groups.add(Group.objects.get(name=ROLE_GENERAL_ADVISOR))
    for sentinel in (-1, 0):
        with pytest.raises(IdentityError):
            AdvisorPrincipal.for_staff(_request(staff), subject_student_id=sentinel)


def test_a_student_with_no_linked_identity_fails_closed():
    """The alternative is a reduced-context answer that looks like a real one.

    A generic reply about university regulations, with nothing to say it was not
    about this student, is worse than an error — the student cannot tell.
    """
    ensure_role_groups()
    from django.contrib.auth.models import Group

    from core.services.rbac import ROLE_STUDENT as R

    user = User.objects.create_user("2000001", password="x")
    user.set_unusable_password()
    user.save()
    user.groups.add(Group.objects.get(name=R))
    set_user_scope(user.id, student_id=None)

    with pytest.raises(IdentityError):
        AdvisorPrincipal.for_student(_request(user))


def test_an_anonymous_request_is_refused():
    from django.contrib.auth.models import AnonymousUser

    ensure_role_groups()
    with pytest.raises(IdentityError):
        AdvisorPrincipal.for_student(_request(AnonymousUser()))


# ── evidence ─────────────────────────────────────────────────────


def _evidence(**overrides) -> dict:
    base = {
        "question": "كم مرة أقدر أنسحب؟",
        "assistant_answer": "خمسة انسحابات.",
        "answer_mode": "PARTIAL",
        "final_disposition": "ESCALATE",
        "reason_codes": ["PROHIBITED_FOR_DECISION"],
        "relevant_student_facts": {"student_id": MINE, "program": "CS"},
        "citations": [
            {
                "policy_id": "TU.WITHDRAWAL.MAXIMUM",
                "document_title": "الدليل",
                "edition": "1447",
                "page": "24",
                "effective_from": "1447",
                "effective_to": "",
            }
        ],
        "missing_information": ["عدد الانسحابات السابقة"],
    }
    base.update(overrides)
    return base


def test_a_complete_snapshot_is_accepted():
    assert validate_evidence(_evidence()) is not None


def test_the_allowlist_is_exactly_the_agreed_fields():
    assert set(_evidence()) == set(EVIDENCE_FIELDS)


def test_a_field_nobody_asked_for_is_refused_on_the_way_in():
    """Refused at write time. Caught at read time it is already stored, already
    backed up, and possibly already read."""
    with pytest.raises(EvidenceError, match="no adviser asked for"):
        validate_evidence(_evidence(tool_results=[{"table": "StudentCourse"}]))


@pytest.mark.parametrize(
    "leak",
    [
        {"judge_findings": ["citation_integrity: fail"]},
        {"reasoning": "first I considered..."},
        {"prompt_version": "v7"},
        {"background_policies": ["TU.OTHER.RULE"]},
    ],
)
def test_the_named_exclusions_are_all_refused(leak):
    with pytest.raises(EvidenceError):
        validate_evidence(_evidence(**leak))


def test_an_unrelated_student_attribute_is_refused():
    with pytest.raises(EvidenceError, match="student facts outside"):
        validate_evidence(
            _evidence(relevant_student_facts={"student_id": MINE, "national_id": "1234567890"})
        )


def test_a_citation_may_not_smuggle_extra_material():
    with pytest.raises(EvidenceError, match="citation carries extra"):
        validate_evidence(
            _evidence(
                citations=[
                    {
                        "policy_id": "TU.X.Y",
                        "document_title": "د",
                        "edition": "1",
                        "page": "1",
                        "effective_from": "",
                        "effective_to": "",
                        "operator_notes": "row count 159,778",
                    }
                ]
            )
        )


# ── the summary must survive the model being down ────────────────


def test_the_summary_needs_no_model():
    summary = deterministic_summary(_evidence())
    assert "كم مرة أقدر أنسحب؟" in summary
    assert "PROHIBITED_FOR_DECISION" in summary
    assert "عدد الانسحابات السابقة" in summary
    assert "TU.WITHDRAWAL.MAXIMUM" in summary


def test_the_summary_survives_a_snapshot_with_nothing_in_it():
    """The model being down is frequently WHY the turn was escalated, so an
    escalation that could not be summarised must not be an escalation that
    could not be raised."""
    summary = deterministic_summary({})
    assert summary.strip()
    assert "لا توجد" in summary


def test_the_summary_does_not_reproduce_the_whole_answer():
    summary = deterministic_summary(_evidence(assistant_answer="ب" * 2000))
    assert len(summary) < 1200
    assert "…" in summary


def test_no_citations_is_stated_rather_than_left_blank():
    """Silence reads as "sources omitted"; the adviser needs "there were none"."""
    assert "لا توجد" in deterministic_summary(_evidence(citations=[]))

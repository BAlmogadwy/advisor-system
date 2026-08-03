"""Every identity path fails closed.

These cover the shapes rather than the instances: an absent scope, a negative id,
a staff edit that touches a student's row, a student record that has gone away.
Each of them was, until this change, a quiet promotion — to super admin, to
student number one, to nobody, or to a permanent error.
"""

from __future__ import annotations

import json
from unittest import mock

import pytest
from django.urls import reverse

from core.models import AdvisorConversation, AdvisorMessage, Student, UserScope
from core.services.rbac import ROLE_STUDENT, ensure_role_groups, get_user_scope, set_user_scope
from core.services.virtual_advisor import _apply_student_scope

pytestmark = pytest.mark.django_db

MINE = 9101001


def _student(client=None, student_id: int = MINE):
    from core.services import student_otp

    ensure_role_groups()
    Student.objects.get_or_create(
        student_id=student_id,
        defaults={"name": f"S{student_id}", "program": "CS", "section": "M"},
    )
    user = student_otp.provision_student_user(student_id)
    if client is not None:
        client.force_login(user)
    return user


# ── an unnamed caller is not a privileged caller ─────────────────


def test_a_call_with_no_scope_reaches_no_student():
    """`(scope or {}).get("role") or ROLE_SUPER_ADMIN` made an unfiltered cohort
    query one dropped keyword argument away from every caller."""
    _student()
    qs, applied = _apply_student_scope(Student.objects.all(), None)
    assert list(qs) == []
    assert applied["role"] == ""


def test_an_empty_scope_reaches_no_student():
    _student()
    qs, applied = _apply_student_scope(Student.objects.all(), {})
    assert list(qs) == []
    assert applied["role"] == ""


# ── a refusal must not resolve to a real person ──────────────────


@pytest.mark.parametrize("sentinel", [-1, 0, None, ""])
def test_a_non_positive_student_id_matches_nobody(sentinel):
    """The WhatsApp gateway used -1 to mean "no student here".

    `_coerce_int(..., minimum=1)` clamped that to 1, so a denial resolved to
    student number one's record.
    """
    _student(student_id=1)
    qs, _applied = _apply_student_scope(
        Student.objects.all(), {"role": ROLE_STUDENT, "student_id": sentinel}
    )
    assert list(qs) == []


def test_a_real_id_still_reaches_exactly_that_student():
    """The positive control: refusing -1 must not refuse everyone."""
    _student()
    qs, _ = _apply_student_scope(Student.objects.all(), {"role": ROLE_STUDENT, "student_id": MINE})
    assert [s.student_id for s in qs] == [MINE]


def test_the_whatsapp_gateway_no_longer_signals_denial_with_a_negative_id():
    """The gateway's "no student here" fallback used -1.

    A negative sentinel travels in the same field a real id occupies, so it only
    stays a refusal for as long as nothing downstream clamps it.
    """
    from whatsapp_gateway.models import WhatsAppUserLink
    from whatsapp_gateway.services import scope_for_link

    unlinked = WhatsAppUserLink(role="UNRECOGNISED", user_id=None, student_id=None)
    scope = scope_for_link(unlinked)
    assert scope.get("student_id") is None

    # And the scope layer refuses it either way.
    qs, _ = _apply_student_scope(Student.objects.all(), scope)
    assert list(qs) == []


# ── an unrelated edit must not erase who someone is ──────────────


def test_setting_a_users_departments_does_not_unlink_their_student_record():
    """`student_id` sat in `update_or_create` defaults with a None default, so
    every staff path that set departments silently NULLed it — and the symptom
    was that the student's every adviser request began to 403."""
    user = _student()
    assert get_user_scope(user).get("student_id") == MINE

    set_user_scope(user.id, advisor_id="A1", departments="CS,IS")

    assert UserScope.objects.get(user_id=user.id).student_id == MINE
    assert get_user_scope(user).get("student_id") == MINE


def test_the_link_can_still_be_cleared_when_that_is_what_is_meant():
    user = _student()
    set_user_scope(user.id, student_id=None)
    assert UserScope.objects.get(user_id=user.id).student_id is None


# ── a vanished record is reported, not swallowed ─────────────────


def test_a_student_whose_record_was_reimported_away_is_told_so(client):
    """Passing a real identity makes the context builder raise where the
    general-mode stub used to answer blandly and work.

    Without its own branch the blanket `except Exception` turns that into a 503
    with the question marked FAILED — on every question, for ever, with no
    explanation. A roster CSV re-import makes it reachable.
    """
    _student(client)
    conversation = AdvisorConversation.objects.create(student_id=MINE)
    Student.objects.filter(student_id=MINE).delete()

    # Nothing is stubbed: the real entry point loads the real record and raises
    # before it ever reaches a model, which is precisely the path under test.
    llm = mock.Mock(side_effect=AssertionError("the model must never be reached"))
    with mock.patch("core.services.virtual_advisor.LocalLLMClient", llm):
        response = client.post(
            reverse("advisor_conversation_send", args=[str(conversation.id)]),
            data=json.dumps({"message": "سؤال"}),
            content_type="application/json",
        )

    assert response.status_code == 409, response.content
    assert "عمادة القبول والتسجيل" in response.json()["error"]
    # The question is kept and marked, not silently dropped.
    assert AdvisorMessage.objects.get(role=AdvisorMessage.ROLE_STUDENT).status == (
        AdvisorMessage.STATUS_FAILED
    )
    # And on no account answered from the general context instead: no assistant turn.
    assert AdvisorMessage.objects.filter(role=AdvisorMessage.ROLE_ASSISTANT).count() == 0


# ── the resolvers deny on their own, not only because the registry does ──


def test_an_unrecognised_role_cannot_resolve_a_student_id():
    """Unreachable today: the registry checks `allowed_roles` before any executor
    runs, and no role set contains the empty string. Tested anyway, because an open
    fall-through means the restriction rests on the REGISTRY rather than on the
    resolver — safe by arrangement rather than by construction, and one refactor
    away from being neither.
    """
    from core.services.virtual_advisor_capabilities import _resolve_scoped_student_id

    # The student must EXIST, or the resolver denies at its row lookup and the
    # assertion below passes without the fall-through being reached at all.
    _student()
    resolved, error = _resolve_scoped_student_id({"student_id": MINE}, {"role": ""})
    assert resolved is None, "an unnamed role resolved a real student record"
    assert error


def test_an_unrecognised_role_cannot_aggregate_over_programmes():
    from core.services.virtual_advisor_capabilities import _resolve_scoped_programs

    programs, error = _resolve_scoped_programs({"programs": ["CS", "AI"]}, {"role": ""})
    assert programs == []
    assert error


def test_a_super_admin_still_resolves_normally():
    """The positive control: closing the fall-through must not close the door."""
    from core.services.rbac import ROLE_SUPER_ADMIN
    from core.services.virtual_advisor_capabilities import (
        _resolve_scoped_programs,
        _resolve_scoped_student_id,
    )

    _student()
    resolved, error = _resolve_scoped_student_id({"student_id": MINE}, {"role": ROLE_SUPER_ADMIN})
    assert (resolved, error) == (MINE, None)
    programs, error = _resolve_scoped_programs({"programs": ["CS"]}, {"role": ROLE_SUPER_ADMIN})
    assert (programs, error) == (["CS"], None)

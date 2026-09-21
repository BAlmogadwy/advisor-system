from __future__ import annotations

import importlib
from datetime import timedelta

import pytest
from django.contrib.auth.models import Group, User
from django.contrib.sessions.middleware import SessionMiddleware
from django.utils import timezone

from core.models import Student, UserScope
from core.services.rbac import ROLE_STUDENT, ensure_role_groups, set_user_scope
from core.services.student_otp import mark_student_authentication, provision_student_user
from telegram_gateway import linking
from telegram_gateway.models import TelegramLink, TelegramLinkToken, TelegramUpdateReceipt

pytestmark = pytest.mark.django_db

SID = 7301001
CHAT = 557301001


def _student_account(student_id: int = SID) -> User:
    Student.objects.get_or_create(
        student_id=student_id,
        defaults={"name": "Lifecycle Student", "program": "CS", "section": "M"},
    )
    return provision_student_user(student_id)


def _additional_student_account(*, student_id: int, username: str) -> User:
    ensure_role_groups()
    user = User.objects.create_user(username=username)
    user.set_unusable_password()
    user.save(update_fields=["password"])
    user.groups.add(Group.objects.get(name=ROLE_STUDENT))
    set_user_scope(user.pk, student_id=student_id)
    return user


def _link(user: User, *, student_id: int = SID, chat_id: int = CHAT) -> TelegramLink:
    return TelegramLink.objects.create(
        telegram_user_id=chat_id,
        student_id=student_id,
        university_user=user,
    )


def _recent_request(rf, user: User):
    request = rf.post("/telegram/link/test/confirm/")
    SessionMiddleware(lambda req: None).process_request(request)
    request.session.save()
    request.user = user
    mark_student_authentication(request)
    return request


def _approve(rf, user: User, *, chat_id: int = CHAT):
    issued = linking.issue_link_token(telegram_user_id=chat_id)
    code = linking.approve_link(
        request=_recent_request(rf, user),
        raw_token=issued.raw_token,
    )
    return TelegramLinkToken.objects.get(token_hash=linking.hash_token(issued.raw_token)), code


@pytest.mark.parametrize("flag", ["is_active", "is_staff", "is_superuser"])
def test_account_flags_revoke_link_fail_closed(flag: str):
    user = _student_account()
    link = _link(user)
    setattr(user, flag, False if flag == "is_active" else True)
    user.save(update_fields=[flag])

    assert linking.active_link_for_chat(CHAT) is None
    assert linking.active_link_by_id(link.pk) is None
    link.refresh_from_db()
    assert link.status == TelegramLink.STATUS_REVOKED
    assert link.revoked_at is not None


def test_removing_student_group_revokes_link():
    user = _student_account()
    link = _link(user)
    user.groups.clear()

    assert linking.active_link_for_chat(CHAT) is None
    link.refresh_from_db()
    assert link.status == TelegramLink.STATUS_REVOKED


@pytest.mark.parametrize("scope_change", ["mismatch", "delete"])
def test_scope_change_revokes_link(scope_change: str):
    user = _student_account()
    link = _link(user)
    scope = UserScope.objects.get(user=user)
    if scope_change == "delete":
        scope.delete()
    else:
        scope.student_id = SID + 1
        scope.save(update_fields=["student_id"])

    assert linking.active_link_for_chat(CHAT) is None
    link.refresh_from_db()
    assert link.status == TelegramLink.STATUS_REVOKED


def test_missing_student_row_revokes_link():
    user = _student_account()
    link = _link(user)
    Student.objects.filter(student_id=SID).delete()

    assert linking.active_link_for_chat(CHAT) is None
    link.refresh_from_db()
    assert link.status == TelegramLink.STATUS_REVOKED


def test_deleted_and_recreated_user_does_not_revive_old_link():
    original = _student_account()
    original_pk = original.pk
    link = _link(original)
    original.delete()

    replacement = provision_student_user(SID)
    assert replacement.pk != original_pk
    assert linking.active_link_for_chat(CHAT) is None

    link.refresh_from_db()
    assert link.status == TelegramLink.STATUS_REVOKED
    assert link.university_user_id is None


def test_approval_and_link_keep_the_exact_authenticated_account(rf):
    user = _student_account()
    token, code = _approve(rf, user)

    assert token.approved_student_id == SID
    assert token.approved_user_id == user.pk

    link = linking.confirm_link(telegram_user_id=CHAT, code=code)
    assert link.student_id == SID
    assert link.university_user_id == user.pk
    assert linking.active_account_for_link(link).pk == user.pk


def test_confirm_does_not_substitute_another_account_for_approved_user(rf):
    approved_user = _student_account()
    token, code = _approve(rf, approved_user)
    _additional_student_account(student_id=SID, username="second-student-account")
    approved_user.is_active = False
    approved_user.save(update_fields=["is_active"])

    with pytest.raises(linking.LinkError) as exc:
        linking.confirm_link(telegram_user_id=CHAT, code=code)

    assert exc.value.code == linking.CONFIRM_INVALID
    token.refresh_from_db()
    assert token.consumed_at is not None
    assert TelegramLink.objects.count() == 0


def test_existing_link_confirmation_consumes_the_code_before_returning(rf):
    user = _student_account()
    existing = _link(user)
    token, code = _approve(rf, user)

    confirmed = linking.confirm_link(telegram_user_id=CHAT, code=code)

    assert confirmed.pk == existing.pk
    token.refresh_from_db()
    assert token.consumed_at is not None
    existing.revoke()
    with pytest.raises(linking.LinkError) as exc:
        linking.confirm_link(telegram_user_id=CHAT, code=code)
    assert exc.value.code == linking.CONFIRM_INVALID


def test_correct_code_blocked_by_a_link_conflict_is_burned(rf):
    user = _student_account()
    waiting_chat = CHAT + 1
    token, code = _approve(rf, user, chat_id=waiting_chat)
    active = _link(user)

    with pytest.raises(linking.LinkError) as exc:
        linking.confirm_link(telegram_user_id=waiting_chat, code=code)
    assert exc.value.code == linking.STUDENT_ALREADY_LINKED
    token.refresh_from_db()
    assert token.consumed_at is not None

    active.revoke()
    with pytest.raises(linking.LinkError) as replay:
        linking.confirm_link(telegram_user_id=waiting_chat, code=code)
    assert replay.value.code == linking.CONFIRM_INVALID


def test_revocation_burns_every_live_ceremony_for_the_link_identity(rf):
    user = _student_account()
    link = _link(user)
    token, _code = _approve(rf, user)
    assert token.consumed_at is None

    link.revoke()

    token.refresh_from_db()
    assert token.consumed_at is not None


def test_stale_conflict_is_revoked_before_new_link_uniqueness_checks(rf):
    Student.objects.create(student_id=SID, name="S", program="CS", section="M")
    stale_user = _additional_student_account(student_id=SID, username="old-account")
    stale_link = _link(stale_user)
    stale_user.is_active = False
    stale_user.save(update_fields=["is_active"])

    current_user = provision_student_user(SID)
    _, code = _approve(rf, current_user)
    stale_link.refresh_from_db()
    assert stale_link.status == TelegramLink.STATUS_REVOKED

    current_link = linking.confirm_link(telegram_user_id=CHAT, code=code)
    assert current_link.university_user_id == current_user.pk
    assert current_link.status == TelegramLink.STATUS_ACTIVE


def test_stale_conflict_revocation_survives_a_different_live_conflict(rf):
    other_sid = SID + 1
    Student.objects.create(student_id=SID, name="S", program="CS", section="M")
    Student.objects.create(student_id=other_sid, name="O", program="CS", section="M")
    stale_user = _additional_student_account(student_id=SID, username="stale-account")
    stale_link = _link(stale_user, chat_id=CHAT + 1)
    stale_user.is_active = False
    stale_user.save(update_fields=["is_active"])

    other_user = provision_student_user(other_sid)
    _link(other_user, student_id=other_sid, chat_id=CHAT)
    current_user = provision_student_user(SID)
    issued = linking.issue_link_token(telegram_user_id=CHAT)

    with pytest.raises(linking.LinkError) as exc:
        linking.approve_link(
            request=_recent_request(rf, current_user),
            raw_token=issued.raw_token,
        )

    assert exc.value.code == linking.CHAT_ALREADY_LINKED
    stale_link.refresh_from_db()
    assert stale_link.status == TelegramLink.STATUS_REVOKED


def test_account_binding_data_migration_fails_closed_for_every_legacy_credential():
    _student_account()
    valid_link = TelegramLink.objects.create(telegram_user_id=CHAT, student_id=SID)
    valid_token = TelegramLinkToken.objects.create(
        token_hash="a" * 64,
        telegram_user_id=CHAT,
        expires_at=timezone.now() + timedelta(minutes=5),
        approved_student_id=SID,
        approved_at=timezone.now(),
    )
    invalid_link = TelegramLink.objects.create(
        telegram_user_id=CHAT + 1,
        student_id=SID + 1,
    )
    invalid_token = TelegramLinkToken.objects.create(
        token_hash="b" * 64,
        telegram_user_id=CHAT + 1,
        expires_at=timezone.now() + timedelta(minutes=5),
        approved_student_id=SID + 1,
        approved_at=timezone.now(),
    )
    ambiguous_sid = SID + 2
    Student.objects.create(student_id=ambiguous_sid, name="A", program="CS", section="M")
    _additional_student_account(student_id=ambiguous_sid, username="ambiguous-one")
    _additional_student_account(student_id=ambiguous_sid, username="ambiguous-two")
    ambiguous_link = TelegramLink.objects.create(
        telegram_user_id=CHAT + 2,
        student_id=ambiguous_sid,
    )
    ambiguous_token = TelegramLinkToken.objects.create(
        token_hash="e" * 64,
        telegram_user_id=CHAT + 2,
        expires_at=timezone.now() + timedelta(minutes=5),
        approved_student_id=ambiguous_sid,
        approved_at=timezone.now(),
    )

    migration = importlib.import_module("telegram_gateway.migrations.0003_account_binding")
    from django.apps import apps

    migration.revoke_unverifiable_legacy_credentials(apps, None)

    valid_link.refresh_from_db()
    valid_token.refresh_from_db()
    invalid_link.refresh_from_db()
    invalid_token.refresh_from_db()
    ambiguous_link.refresh_from_db()
    ambiguous_token.refresh_from_db()
    assert valid_link.status == TelegramLink.STATUS_REVOKED
    assert valid_link.university_user_id is None
    assert valid_token.approved_user_id is None
    assert valid_token.consumed_at is not None
    assert invalid_link.status == TelegramLink.STATUS_REVOKED
    assert invalid_token.consumed_at is not None
    assert ambiguous_link.status == TelegramLink.STATUS_REVOKED
    assert ambiguous_link.university_user_id is None
    assert ambiguous_token.consumed_at is not None


def test_admin_exposes_account_binding_as_read_only_only():
    from django.contrib import admin as django_admin

    from telegram_gateway.admin import TelegramLinkAdmin

    model_admin = TelegramLinkAdmin(TelegramLink, django_admin.site)
    assert "university_user" in model_admin.readonly_fields
    assert "telegram_user_id" in model_admin.exclude
    assert model_admin.actions == ("revoke_selected_links",)


def test_purge_keeps_live_tokens_and_nonterminal_jobs():
    now = timezone.now()
    old = now - timedelta(days=8)

    live_token = TelegramLinkToken.objects.create(
        token_hash="c" * 64,
        telegram_user_id=CHAT,
        expires_at=now + timedelta(days=1),
    )
    dead_token = TelegramLinkToken.objects.create(
        token_hash="d" * 64,
        telegram_user_id=CHAT + 1,
        expires_at=now - timedelta(days=1),
    )
    consumed_token = TelegramLinkToken.objects.create(
        token_hash="f" * 64,
        telegram_user_id=CHAT + 2,
        expires_at=now + timedelta(days=1),
        consumed_at=now - timedelta(days=1),
    )
    TelegramLinkToken.objects.filter(
        pk__in=[live_token.pk, dead_token.pk, consumed_token.pk]
    ).update(created_at=old)

    queued = TelegramUpdateReceipt.objects.create(
        update_id=88001,
        status=TelegramUpdateReceipt.STATUS_QUEUED,
    )
    running = TelegramUpdateReceipt.objects.create(
        update_id=88002,
        status=TelegramUpdateReceipt.STATUS_RUNNING,
    )
    terminal = TelegramUpdateReceipt.objects.create(
        update_id=88003,
        status=TelegramUpdateReceipt.STATUS_SUCCEEDED,
    )
    recently_finished = TelegramUpdateReceipt.objects.create(
        update_id=88004,
        kind=TelegramUpdateReceipt.KIND_QUESTION,
        status=TelegramUpdateReceipt.STATUS_SUCCEEDED,
        finished_at=now,
    )
    old_finished = TelegramUpdateReceipt.objects.create(
        update_id=88005,
        kind=TelegramUpdateReceipt.KIND_QUESTION,
        status=TelegramUpdateReceipt.STATUS_FAILED,
        finished_at=old,
    )
    TelegramUpdateReceipt.objects.filter(
        pk__in=[queued.pk, running.pk, terminal.pk, recently_finished.pk]
    ).update(
        received_at=old,
    )

    linking.purge_expired()

    assert TelegramLinkToken.objects.filter(pk=live_token.pk).exists()
    assert not TelegramLinkToken.objects.filter(pk=dead_token.pk).exists()
    assert not TelegramLinkToken.objects.filter(pk=consumed_token.pk).exists()
    assert TelegramUpdateReceipt.objects.filter(pk=queued.pk).exists()
    assert TelegramUpdateReceipt.objects.filter(pk=running.pk).exists()
    assert not TelegramUpdateReceipt.objects.filter(pk=terminal.pk).exists()
    assert TelegramUpdateReceipt.objects.filter(pk=recently_finished.pk).exists()
    assert not TelegramUpdateReceipt.objects.filter(pk=old_finished.pk).exists()

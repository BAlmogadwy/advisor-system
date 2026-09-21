"""Administrator revocation, and deliberately nothing else.

Staff need one capability here: cut a link off — a reported lost handset, a
student who says the bot is answering somebody else. They do not need to browse
which students use Telegram, and they must not be able to CREATE a link, because a
link created by staff is one that was never confirmed inside the student's own
session, which is the single fact the whole authentication design rests on.

So: no add, no edit, no delete, one action. Tokens and receipts are not registered
at all — a token is a credential and a receipt is an idempotency key, and neither
is something to read in a changelist.
"""

from __future__ import annotations

from typing import Any

from django.contrib import admin, messages
from django.db.models import QuerySet
from django.http import HttpRequest

from .models import TelegramLink


@admin.register(TelegramLink)
class TelegramLinkAdmin(admin.ModelAdmin):
    """Read and revoke. Never create, never edit."""

    list_display = ("id", "student_id", "status", "linked_at", "revoked_at", "last_seen_at")
    list_filter = ("status",)
    # By student, which is the identifier an administrator acting on a support
    # request actually holds. Searching by Telegram id is not offered: it would
    # require staff to obtain one, and there is no legitimate workflow that starts
    # with a Telegram id.
    search_fields = ("student_id",)
    ordering = ("-linked_at",)
    actions = ("revoke_selected_links",)

    #: The Telegram id is in the table because delivery needs it, and out of every
    #: staff-facing surface because nothing staff do requires reading it.
    exclude = ("telegram_user_id",)
    readonly_fields = (
        "id",
        "student_id",
        "university_user",
        "status",
        "current_conversation",
        "linked_at",
        "revoked_at",
        "last_seen_at",
    )

    def has_add_permission(self, request: HttpRequest) -> bool:
        # A link minted here would never have passed through a student's own
        # authenticated session — the one thing that makes it mean anything.
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        # Revocation is the action; re-pointing a link at a different student by
        # editing a field is exactly the cross-student reuse the constraints exist
        # to prevent.
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        # Revoked rows are the evidence that a link existed. Deleting them removes
        # the only record that a chat once had access.
        return False

    def has_revoke_permission(self, request: HttpRequest) -> bool:
        """Use the model's change grant to authorise the one safe mutation.

        ``has_change_permission`` stays false so Django never exposes the edit
        form. A custom action permission keeps revocation available to operators
        who were deliberately granted change rights without also granting field
        editing.
        """

        return bool(
            request.user.is_active
            and request.user.is_staff
            and request.user.has_perm("telegram_gateway.change_telegramlink")
        )

    @admin.action(
        permissions=["revoke"],
        description="Revoke the selected Telegram links (immediate)",
    )
    def revoke_selected_links(self, request: HttpRequest, queryset: QuerySet[TelegramLink]) -> None:
        from . import linking

        revoked = linking.revoke_links(queryset)
        if revoked:
            from .jobs import cancel_jobs_for_revoked_links

            cancel_jobs_for_revoked_links()
        self.message_user(
            request,
            f"{revoked} Telegram link(s) revoked. Access ends immediately.",
            messages.SUCCESS if revoked else messages.WARNING,
        )

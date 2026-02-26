from argparse import ArgumentParser
from typing import Any

from django.contrib.auth.models import Group, User
from django.core.management.base import BaseCommand

from core.services.rbac import (
    ROLE_ADVISOR,
    ROLE_GENERAL_ADVISOR,
    ROLE_SUPER_ADMIN,
    ensure_role_groups,
    ensure_scope_schema,
    set_user_scope,
)


class Command(BaseCommand):
    help = "Seed RBAC roles and optionally assign user scope"

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument("username")
        parser.add_argument(
            "--role", choices=[ROLE_SUPER_ADMIN, ROLE_GENERAL_ADVISOR, ROLE_ADVISOR], required=True
        )
        parser.add_argument("--advisor-id", default="")
        parser.add_argument("--departments", default="")

    def handle(self, *args: Any, **opts: Any) -> None:
        ensure_role_groups()
        ensure_scope_schema()

        username = opts["username"]
        role = opts["role"]
        advisor_id = opts["advisor_id"]
        departments = opts["departments"]

        user = User.objects.filter(username=username).first()
        if not user:
            self.stdout.write(self.style.ERROR(f"User not found: {username}"))
            return

        user.groups.clear()
        g = Group.objects.get(name=role)
        user.groups.add(g)
        set_user_scope(user.id, advisor_id=advisor_id, departments=departments)

        self.stdout.write(self.style.SUCCESS(f"Assigned {username} -> {role}"))

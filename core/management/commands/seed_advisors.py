"""Auto-register advisors from scraped student data.

Reads distinct advisor names from the students table and creates:
- AcademicAdvisor records
- Django User accounts (username = first_second Arabic name parts)
- ADVISOR role assignment + UserScope linking

Password resolution order:
  1. --password CLI argument
  2. SEED_ADVISOR_PASSWORD environment variable
  3. Randomly generated 12-char token (printed to stdout)
"""

import io
import os
import secrets
import sys
from typing import Any

from django.contrib.auth.models import Group, User
from django.core.management.base import BaseCommand
from django.db import transaction

from core.models import Student
from core.services.advisors import normalize_arabic, upsert_academic_advisor
from core.services.rbac import (
    ROLE_ADVISOR,
    ensure_role_groups,
    set_user_scope,
)


def _build_username_map(advisor_names: list[str]) -> dict[str, str]:
    """Map each full advisor name to a unique username (firstname_secondname).

    Always uses first two name parts for safety. If still duplicate,
    appends an incrementing suffix.
    """
    username_map: dict[str, str] = {}
    seen_usernames: dict[str, str] = {}  # normalised username -> full_name

    for full_name in advisor_names:
        parts = full_name.split()
        # Always use first_second format
        if len(parts) >= 2:
            candidate = f"{parts[0]}_{parts[1]}"
        else:
            candidate = parts[0] if parts else full_name

        # Check for collision (normalise for comparison)
        norm = normalize_arabic(candidate)
        if norm in seen_usernames:
            # Add incrementing suffix
            i = 2
            while normalize_arabic(f"{candidate}_{i}") in seen_usernames:
                i += 1
            candidate = f"{candidate}_{i}"
            norm = normalize_arabic(candidate)

        seen_usernames[norm] = full_name
        username_map[full_name] = candidate

    # Final check against existing Django users
    for full_name, username in list(username_map.items()):
        original = username
        suffix = 2
        while User.objects.filter(username=username).exists():
            username = f"{original}_{suffix}"
            suffix += 1
        username_map[full_name] = username

    return username_map


class Command(BaseCommand):
    help = "Auto-register advisors from scraped student data"

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Preview without making changes",
        )
        parser.add_argument(
            "--password",
            type=str,
            default=None,
            help="Password for all seeded advisor accounts (overrides SEED_ADVISOR_PASSWORD env var)",
        )

    def handle(self, *args: Any, **opts: Any) -> None:
        # Force UTF-8 stdout on Windows to handle Arabic names
        if sys.platform == "win32" and not isinstance(self.stdout, io.TextIOWrapper):
            self.stdout._out = io.TextIOWrapper(  # type: ignore[attr-defined]
                self.stdout._out.buffer,  # type: ignore[attr-defined]
                encoding="utf-8",
                errors="replace",
            )

        dry_run = opts["dry_run"]

        # Resolve password: CLI flag > env var > random
        password = opts.get("password") or os.getenv("SEED_ADVISOR_PASSWORD") or None
        password_generated = False
        if not password:
            password = secrets.token_urlsafe(9)  # 12-char URL-safe random string
            password_generated = True

        if password_generated:
            self.stdout.write(
                self.style.WARNING(f"No password provided. Generated random password: {password}")
            )
            self.stdout.write(
                self.style.WARNING("Save this password now -- it will not be shown again.")
            )

        # 1. Ensure role groups exist
        ensure_role_groups()

        # 2. Get distinct advisor names from students
        advisor_names = list(
            Student.objects.exclude(advisor_id="")
            .values_list("advisor_id", flat=True)
            .distinct()
            .order_by("advisor_id")
        )

        if not advisor_names:
            self.stdout.write(self.style.WARNING("No advisors found in student data."))
            return

        self.stdout.write(f"Found {len(advisor_names)} distinct advisors.\n")

        # 3. Build username map
        username_map = _build_username_map(advisor_names)

        # 4. Auto-detect departments per advisor
        dept_map: dict[str, str] = {}
        for name in advisor_names:
            programs = list(
                Student.objects.filter(advisor_id=name)
                .exclude(program="")
                .values_list("program", flat=True)
                .distinct()
            )
            dept_map[name] = (
                ";".join(sorted(p for p in programs if p is not None)) if programs else ""
            )

        # 5. Print preview table
        self.stdout.write(f"\n{'#':>3}  {'Username':<25} {'Department':<15} {'Full Name'}")
        self.stdout.write("-" * 90)

        for i, name in enumerate(advisor_names, 1):
            username = username_map[name]
            dept = dept_map.get(name, "")
            self.stdout.write(f"{i:>3}  {username:<25} {dept:<15} {name}")

        self.stdout.write("")

        if dry_run:
            self.stdout.write(self.style.WARNING("Dry run — no changes made."))
            return

        # 6. Create everything in a transaction
        created = 0
        skipped = 0
        advisor_group = Group.objects.get(name=ROLE_ADVISOR)

        with transaction.atomic():
            for i, name in enumerate(advisor_names, 1):
                username = username_map[name]
                dept = dept_map.get(name, "")

                # Create AcademicAdvisor record
                upsert_academic_advisor(
                    advisor_id=name,
                    full_name=name,
                    email=f"advisor{i:02d}@placeholder.local",
                    department=dept,
                )

                # Create Django User
                if User.objects.filter(username=username).exists():
                    self.stdout.write(
                        self.style.WARNING(f"  User '{username}' already exists — skipped")
                    )
                    skipped += 1
                    continue

                user = User.objects.create_user(
                    username=username,
                    password=password,
                )
                user.groups.add(advisor_group)
                set_user_scope(user.id, advisor_id=name, departments=dept)
                created += 1

        self.stdout.write(
            self.style.SUCCESS(f"Done: {created} advisors created, {skipped} skipped.")
        )

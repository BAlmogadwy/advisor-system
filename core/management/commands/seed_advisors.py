"""Auto-register advisors from scraped student data.

Reads distinct advisor names from the students table and:
- Assigns each advisor an auto-increment integer ID (1, 2, 3, ...)
- Creates AcademicAdvisor records with the integer ID
- Creates Django User accounts (username = first_second Arabic name parts)
- Links User → AcademicAdvisor via UserScope(advisor_id=integer_id)
- Replaces all Arabic names in Student.advisor_id with the integer ID

Re-scrape safe: if students already have integer IDs from a previous run,
those are left untouched.  Only new Arabic names are processed.  If an Arabic
name matches an existing AcademicAdvisor.full_name, the same integer ID is
reused instead of creating a duplicate.

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

from core.models import AcademicAdvisor, Student
from core.services.advisors import transliterate_arabic, upsert_academic_advisor
from core.services.rbac import (
    ROLE_ADVISOR,
    ensure_role_groups,
    set_user_scope,
)


def _is_integer_id(value: str) -> bool:
    """Return True if *value* looks like an already-assigned integer ID."""
    try:
        int(value)
        return True
    except (ValueError, TypeError):
        return False


def _build_username_map(advisor_names: list[str]) -> dict[str, str]:
    """Map each full Arabic advisor name to a unique English username (firstname_secondname).

    Transliterates Arabic to English, then uses first two name parts.
    If still duplicate, appends an incrementing suffix.
    """
    username_map: dict[str, str] = {}
    seen_usernames: dict[str, str] = {}  # lowercase username -> full_name

    for full_name in advisor_names:
        # Transliterate to English, then split
        english = transliterate_arabic(full_name)
        parts = english.split()
        # Use first_second format
        if len(parts) >= 2:
            candidate = f"{parts[0]}_{parts[1]}"
        else:
            candidate = parts[0] if parts else english or "advisor"

        # Check for collision
        key = candidate.lower()
        if key in seen_usernames:
            # Add incrementing suffix
            i = 2
            while f"{candidate}_{i}".lower() in seen_usernames:
                i += 1
            candidate = f"{candidate}_{i}"
            key = candidate.lower()

        seen_usernames[key] = full_name
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
        # Skip when called via call_command() with StringIO (no buffer attribute)
        if sys.platform == "win32" and not isinstance(self.stdout, io.TextIOWrapper):
            inner = getattr(self.stdout, "_out", None)
            if inner and hasattr(inner, "buffer"):
                self.stdout._out = io.TextIOWrapper(  # type: ignore[attr-defined]
                    inner.buffer,
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

        # 2. Get ALL distinct advisor_id values from students
        all_advisor_values = list(
            Student.objects.exclude(advisor_id="")
            .values_list("advisor_id", flat=True)
            .distinct()
            .order_by("advisor_id")
        )

        if not all_advisor_values:
            self.stdout.write(self.style.WARNING("No advisors found in student data."))
            return

        # 3. Separate already-converted integer IDs from Arabic names
        arabic_names: list[str] = []
        existing_int_ids: list[int] = []
        for val in all_advisor_values:
            if _is_integer_id(val):
                existing_int_ids.append(int(val))
            else:
                arabic_names.append(val)

        if existing_int_ids:
            self.stdout.write(
                f"Found {len(existing_int_ids)} advisors already using integer IDs (skipped)."
            )

        if not arabic_names:
            self.stdout.write(
                self.style.SUCCESS("All advisors already have integer IDs. Nothing to do.")
            )
            return

        self.stdout.write(f"Found {len(arabic_names)} Arabic advisor names to process.\n")

        # 4. Look up existing AcademicAdvisor records to reuse IDs for known names
        existing_advisors: dict[str, str] = {}  # full_name -> advisor_id
        for adv in AcademicAdvisor.objects.all().values_list("advisor_id", "full_name"):
            existing_advisors[adv[1]] = adv[0]

        # 5. Build ID map: reuse existing IDs for known names, assign new ones for new names
        max_existing_id = max(existing_int_ids) if existing_int_ids else 0
        # Also consider IDs from existing AcademicAdvisor records
        for adv_id_str in existing_advisors.values():
            if _is_integer_id(adv_id_str):
                max_existing_id = max(max_existing_id, int(adv_id_str))

        id_map: dict[str, str] = {}  # arabic_name -> integer ID string
        next_id = max_existing_id + 1

        for name in arabic_names:
            if name in existing_advisors and _is_integer_id(existing_advisors[name]):
                # Reuse the existing integer ID
                id_map[name] = existing_advisors[name]
            else:
                # Assign a new integer ID
                id_map[name] = str(next_id)
                next_id += 1

        reused = sum(
            1
            for n in arabic_names
            if n in existing_advisors and _is_integer_id(existing_advisors[n])
        )
        new_count = len(arabic_names) - reused
        if reused:
            self.stdout.write(
                f"  Reusing {reused} existing advisor IDs, assigning {new_count} new IDs."
            )

        # 6. Build username map (for Django user accounts) — only for Arabic names
        username_map = _build_username_map(arabic_names)

        # 7. Auto-detect departments per advisor
        dept_map: dict[str, str] = {}
        for name in arabic_names:
            programs = list(
                Student.objects.filter(advisor_id=name)
                .exclude(program="")
                .values_list("program", flat=True)
                .distinct()
            )
            dept_map[name] = (
                ";".join(sorted(p for p in programs if p is not None)) if programs else ""
            )

        # 8. Count students per advisor (for preview)
        student_counts: dict[str, int] = {}
        for name in arabic_names:
            student_counts[name] = Student.objects.filter(advisor_id=name).count()

        # 9. Print preview table
        self.stdout.write(
            f"\n{'#':>3}  {'ID':<6} {'Status':<8} {'Username':<25} {'Dept':<15} "
            f"{'Students':>8}  {'Full Name'}"
        )
        self.stdout.write("-" * 110)

        for idx, name in enumerate(arabic_names, 1):
            adv_id = id_map[name]
            status = (
                "reuse"
                if (name in existing_advisors and _is_integer_id(existing_advisors.get(name, "")))
                else "NEW"
            )
            username = username_map[name]
            dept = dept_map.get(name, "")
            count = student_counts.get(name, 0)
            self.stdout.write(
                f"{idx:>3}  {adv_id:<6} {status:<8} {username:<25} {dept:<15} {count:>8}  {name}"
            )

        total_students = sum(student_counts.values())
        self.stdout.write(
            f"\nTotal: {len(arabic_names)} advisors to process, {total_students} students\n"
        )

        if dry_run:
            self.stdout.write(self.style.WARNING("Dry run -- no changes made."))
            return

        # 10. Execute everything in a single transaction
        created = 0
        updated = 0
        advisor_group = Group.objects.get(name=ROLE_ADVISOR)

        with transaction.atomic():
            # 10a. Delete old AcademicAdvisor records whose PK is an Arabic name
            #       (they will be recreated with integer IDs below).
            old_arabic_pks = [
                n for n in arabic_names if AcademicAdvisor.objects.filter(advisor_id=n).exists()
            ]
            if old_arabic_pks:
                AcademicAdvisor.objects.filter(advisor_id__in=old_arabic_pks).delete()
                self.stdout.write(
                    f"  Removed {len(old_arabic_pks)} old Arabic-key advisor records."
                )

            for name in arabic_names:
                adv_id = id_map[name]
                username = username_map[name]
                dept = dept_map.get(name, "")

                # 10b. Create/update AcademicAdvisor with integer ID as primary key
                upsert_academic_advisor(
                    advisor_id=adv_id,
                    full_name=name,
                    email=f"advisor{adv_id}@placeholder.local",
                    department=dept,
                )

                # 10c. Replace Arabic name with integer ID in all student records
                replaced = Student.objects.filter(advisor_id=name).update(advisor_id=adv_id)
                if replaced:
                    self.stdout.write(f"  {name[:40]:<40} -> ID {adv_id} ({replaced} students)")

                # 10d. Create Django User (or update existing)
                if User.objects.filter(username=username).exists():
                    # Update existing user's scope to use (potentially new) integer ID
                    user = User.objects.get(username=username)
                    set_user_scope(user.id, advisor_id=adv_id, departments=dept)
                    self.stdout.write(
                        self.style.WARNING(f"  User '{username}' already exists -- updated scope")
                    )
                    updated += 1
                    continue

                user = User.objects.create_user(
                    username=username,
                    password=password,
                )
                user.groups.add(advisor_group)
                set_user_scope(user.id, advisor_id=adv_id, departments=dept)
                created += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"\nDone: {created} advisors created, {updated} updated. "
                f"All Arabic Student.advisor_id values replaced with integer IDs."
            )
        )

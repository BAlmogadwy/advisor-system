"""Import per-section instructor assignments from the registrar's faculty report.

Usage:
    python manage.py import_section_instructors --file "<saved page>.html" --dry-run
    python manage.py import_section_instructors --file "<saved page>.html"
    python manage.py import_section_instructors --file a.html --file b.mhtml

Reads ``facultySectionsAvilableSeats.do`` (the only source that names an
instructor per section) and writes ``SectionInstructor`` rows keyed on the global
natural key ``(course_key, section)``.

This command is **additive and inert**: it creates ``Instructor`` and
``SectionInstructor`` rows only.  It never touches ``TermSectionMeeting.instructor``
and never touches ``CourseInstructor``, so no planner, clash, cap, compaction or
export behaviour changes as a result of running it.
"""

from __future__ import annotations

from argparse import ArgumentParser
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.models import Instructor, SectionInstructor, TermSection
from core.services.faculty_sections_import import (
    ParseResult,
    parse_faculty_sections_file,
    summarise,
)
from core.services.timetable_pr4_instructor import normalise_instructor

SOURCE = "registrar_faculty_sections"


def _console_safe(text: str, stream: Any) -> str:
    """Make ``text`` writable to ``stream``.

    The registrar's files have Arabic names and the Windows console defaults to
    cp1252, so printing a path would raise ``UnicodeEncodeError`` and abort an
    otherwise valid import.  Degrade the display instead of failing the run.
    """
    encoding = getattr(stream, "encoding", None) or "utf-8"
    try:
        text.encode(encoding)
    except UnicodeEncodeError:
        return text.encode(encoding, errors="replace").decode(encoding, errors="replace")
    except LookupError:
        # The stream names a codec that does not exist, so it cannot be used for
        # the fallback either.  Drop to ASCII rather than re-raising the same
        # error the guard was written to absorb.
        return text.encode("ascii", errors="replace").decode("ascii")
    return text


class Command(BaseCommand):
    help = "Import per-section instructor assignments from the registrar faculty-sections report"

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument(
            "--file",
            action="append",
            required=True,
            dest="files",
            help="Saved faculty-sections page (.html or .mhtml). Repeatable.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would change and write nothing.",
        )
        parser.add_argument(
            "--role",
            default="primary",
            choices=("primary", "co", "lab"),
            help="Role to record for the imported assignment (default: primary).",
        )
        parser.add_argument(
            "--only-known-sections",
            action="store_true",
            help=(
                "Import an assignment only where the section already exists locally. "
                "Without it, an assignment for a not-yet-scraped section is kept and "
                "resolves when that section arrives."
            ),
        )

    def handle(self, *args: Any, **options: Any) -> None:
        files: list[str] = list(options["files"])
        dry_run: bool = bool(options["dry_run"])
        role: str = str(options["role"])
        only_known: bool = bool(options["only_known_sections"])

        rows_by_key: dict[tuple[str, str], Any] = {}
        source_of: dict[tuple[str, str], str] = {}
        cross_file_conflicts: list[str] = []
        for path in files:
            try:
                result: ParseResult = parse_faculty_sections_file(path)
            except OSError as exc:
                # FileNotFoundError, IsADirectoryError and PermissionError are all
                # OSError; a saved page's sibling "_files" directory hits the last.
                raise CommandError(
                    f"Cannot read {_console_safe(path, self.stdout._out)}: {exc}"
                ) from exc
            self.stdout.write(_console_safe(path, self.stdout._out))
            for label, value in summarise(result).items():
                self.stdout.write(f"    {label}: {value}")
            if result.duplicates:
                # A real registrar contradiction, not the benign capacity repeat.
                self.stdout.write(
                    self.style.WARNING(
                        f"    {result.duplicates} section(s) published with conflicting "
                        f"instructor/meeting data — those rows were NOT merged."
                    )
                )
            for row in result.rows:
                key = (row.course_key, row.section)
                previous = rows_by_key.get(key)
                if previous is None or (row.has_instructor and not previous.has_instructor):
                    # Later files win, matching the shell habit of listing oldest
                    # first — and a named instructor always beats a blank, so a
                    # report that merely omits a section cannot erase it.
                    rows_by_key[key] = row
                    source_of[key] = path
                elif row.has_instructor and row.instructor != previous.instructor:
                    cross_file_conflicts.append(
                        f"{row.course_key}/{row.section}: "
                        f"{previous.instructor!r} ({_console_safe(source_of[key], self.stdout._out)})"
                        f" -> {row.instructor!r}"
                    )
                    rows_by_key[key] = row
                    source_of[key] = path

        if cross_file_conflicts:
            # Silently resolving these by argument order loses real assignments:
            # on two saved snapshots of the male report, 105 of 305 shared sections
            # disagreed and 22 assignments vanished depending on --file order.
            self.stdout.write(
                self.style.WARNING(
                    f"\n{len(cross_file_conflicts)} section(s) disagree between files; "
                    f"the LAST file listed wins:"
                )
            )
            for line in cross_file_conflicts[:20]:
                self.stdout.write(f"    {line}")
            if len(cross_file_conflicts) > 20:
                self.stdout.write(f"    ... and {len(cross_file_conflicts) - 20} more")

        assignable = [r for r in rows_by_key.values() if r.has_instructor]
        if not assignable:
            raise CommandError("No rows carrying an instructor were found; nothing to import.")

        # Does the section exist locally?  Reported, never enforced — the link is
        # keyed on the registrar's identity, so an assignment for a section we have
        # not scraped yet is valid and resolves the moment that section arrives.
        known = {
            (str(ck).strip().upper(), str(sec).strip().upper())
            for ck, sec in TermSection.objects.filter(scenario__isnull=True).values_list(
                "course_key", "section"
            )
        }
        matched = [r for r in assignable if (r.course_key, r.section) in known]
        if only_known:
            # The operator asked to record only what our own sections can carry.
            # Everything else is dropped here, loudly — a silent narrowing would
            # read as "the report had nothing more to give".
            dropped = len(assignable) - len(matched)
            assignable = matched
            if dropped:
                self.stdout.write(
                    self.style.WARNING(
                        f"--only-known-sections: dropped {dropped} assignment(s) "
                        f"for sections not present locally."
                    )
                )
            if not assignable:
                raise CommandError("No assignment matched a local section; nothing to import.")

        # Key people by the normalised id, not the raw string: two spellings of
        # one person are one creation, and previewing them as two made --dry-run
        # disagree with the run it was previewing.  The raw name is kept only as
        # the display value for a newly created row.
        display_by_norm: dict[str, str] = {}
        for row in assignable:
            norm = normalise_instructor(row.instructor)
            if norm is not None:
                display_by_norm.setdefault(norm, row.instructor)
        names = sorted(display_by_norm)
        existing = set(
            Instructor.objects.filter(normalised_name__in=names).values_list(
                "normalised_name", flat=True
            )
        )
        new_names = [n for n in names if n not in existing]

        self.stdout.write("")
        self.stdout.write(f"sections carrying an instructor : {len(assignable)}")
        self.stdout.write(f"  matching a local section      : {len(matched)}")
        self.stdout.write(
            f"  no local section              : "
            f"{0 if only_known else len(assignable) - len(matched)}"
            f"{' (dropped)' if only_known else ' (kept anyway)'}"
        )
        self.stdout.write(f"distinct instructors            : {len(names)}")
        self.stdout.write(f"  already known                 : {len(names) - len(new_names)}")
        self.stdout.write(f"  to be created                 : {len(new_names)}")

        if dry_run:
            self.stdout.write(self.style.WARNING("\n--dry-run: nothing written."))
            return

        created_people, created_links, updated_links, unchanged = 0, 0, 0, 0
        with transaction.atomic():
            people: dict[str, Instructor] = {}
            for norm in names:
                display = display_by_norm[norm]
                person, was_created = Instructor.objects.get_or_create(
                    normalised_name=norm,
                    defaults={
                        "full_name": display,
                        # The report is Arabic, so the display name IS the Arabic
                        # name; record it in both so the roster reads correctly
                        # whichever field a screen prefers.
                        "full_name_ar": display,
                        "is_active": True,
                    },
                )
                created_people += int(was_created)
                people[norm] = person

            for row in assignable:
                norm = normalise_instructor(row.instructor)
                assigned = people.get(norm) if norm else None
                if assigned is None:
                    continue
                section_rows = SectionInstructor.objects.filter(
                    scenario__isnull=True,
                    course_key=row.course_key,
                    section=row.section,
                )
                # Probe by the identity the unique index actually enforces —
                # (course_key, section, instructor), which carries no role.  A
                # probe on role instead cannot see the row it is about to collide
                # with, so a second run with a different --role, or a registrar
                # promoting an existing co-instructor, raised IntegrityError and
                # rolled back the entire import.
                held_by_assigned = section_rows.filter(instructor=assigned).first()
                holder_of_role = section_rows.filter(role=role).first()

                if held_by_assigned is not None:
                    if held_by_assigned.role == role:
                        unchanged += 1
                        continue
                    # Promote or demote the person already linked to this section.
                    # Free the target role first: the one-primary partial index
                    # permits only one holder at a time.
                    if holder_of_role is not None and holder_of_role.pk != held_by_assigned.pk:
                        holder_of_role.delete()
                    held_by_assigned.role = role
                    held_by_assigned.source = SOURCE
                    held_by_assigned.save(update_fields=["role", "source", "updated_at"])
                    updated_links += 1
                elif holder_of_role is not None:
                    # The registrar reassigned the section to someone new.
                    holder_of_role.instructor = assigned
                    holder_of_role.source = SOURCE
                    holder_of_role.save(update_fields=["instructor", "source", "updated_at"])
                    updated_links += 1
                else:
                    SectionInstructor.objects.create(
                        scenario=None,
                        course_key=row.course_key,
                        section=row.section,
                        instructor=assigned,
                        role=role,
                        source=SOURCE,
                    )
                    created_links += 1

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(f"instructors created : {created_people}"))
        self.stdout.write(self.style.SUCCESS(f"assignments created : {created_links}"))
        self.stdout.write(self.style.SUCCESS(f"assignments updated : {updated_links}"))
        self.stdout.write(f"assignments unchanged: {unchanged}")
        self.stdout.write(
            f"\nsection_instructors total: {SectionInstructor.objects.count()}"
            f"   instructors total: {Instructor.objects.count()}"
        )

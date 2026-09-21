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
    except (UnicodeEncodeError, LookupError):
        return text.encode(encoding, errors="replace").decode(encoding, errors="replace")
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

    def handle(self, *args: Any, **options: Any) -> None:
        files: list[str] = list(options["files"])
        dry_run: bool = bool(options["dry_run"])
        role: str = str(options["role"])

        rows_by_key: dict[tuple[str, str], Any] = {}
        for path in files:
            try:
                result: ParseResult = parse_faculty_sections_file(path)
            except FileNotFoundError as exc:
                raise CommandError(f"No such file: {path}") from exc
            self.stdout.write(_console_safe(path, self.stdout._out))
            for key, value in summarise(result).items():
                self.stdout.write(f"    {key}: {value}")
            if result.duplicates:
                # A real registrar contradiction, not the benign capacity repeat.
                self.stdout.write(
                    self.style.WARNING(
                        f"    {result.duplicates} section(s) published with conflicting "
                        f"instructor/meeting data — those rows were NOT merged."
                    )
                )
            for row in result.rows:
                rows_by_key.setdefault((row.course_key, row.section), row)

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

        names = sorted({r.instructor for r in assignable})
        existing = {
            n: pk
            for n, pk in Instructor.objects.filter(
                normalised_name__in=[normalise_instructor(n) for n in names]
            ).values_list("normalised_name", "pk")
        }
        new_names = [n for n in names if normalise_instructor(n) not in existing]

        self.stdout.write("")
        self.stdout.write(f"sections carrying an instructor : {len(assignable)}")
        self.stdout.write(f"  matching a local section      : {len(matched)}")
        self.stdout.write(f"  no local section (kept anyway): {len(assignable) - len(matched)}")
        self.stdout.write(f"distinct instructors            : {len(names)}")
        self.stdout.write(f"  already known                 : {len(names) - len(new_names)}")
        self.stdout.write(f"  to be created                 : {len(new_names)}")

        if dry_run:
            self.stdout.write(self.style.WARNING("\n--dry-run: nothing written."))
            return

        created_people, created_links, updated_links, unchanged = 0, 0, 0, 0
        with transaction.atomic():
            people: dict[str, Instructor] = {}
            for name in names:
                norm = normalise_instructor(name)
                if norm is None:
                    continue
                person, was_created = Instructor.objects.get_or_create(
                    normalised_name=norm,
                    defaults={
                        "full_name": name,
                        # The report is Arabic, so the display name IS the Arabic
                        # name; record it in both so the roster reads correctly
                        # whichever field a screen prefers.
                        "full_name_ar": name,
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
                existing_primary = SectionInstructor.objects.filter(
                    scenario__isnull=True,
                    course_key=row.course_key,
                    section=row.section,
                    role=role,
                ).first()
                if existing_primary is None:
                    SectionInstructor.objects.create(
                        scenario=None,
                        course_key=row.course_key,
                        section=row.section,
                        instructor=assigned,
                        role=role,
                        source=SOURCE,
                    )
                    created_links += 1
                elif existing_primary.instructor_id != assigned.pk:
                    # The registrar reassigned the section.  Replace rather than
                    # add: the one-primary constraint permits exactly one.
                    existing_primary.instructor = assigned
                    existing_primary.source = SOURCE
                    existing_primary.save(update_fields=["instructor", "source", "updated_at"])
                    updated_links += 1
                else:
                    unchanged += 1

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(f"instructors created : {created_people}"))
        self.stdout.write(self.style.SUCCESS(f"assignments created : {created_links}"))
        self.stdout.write(self.style.SUCCESS(f"assignments updated : {updated_links}"))
        self.stdout.write(f"assignments unchanged: {unchanged}")
        self.stdout.write(
            f"\nsection_instructors total: {SectionInstructor.objects.count()}"
            f"   instructors total: {Instructor.objects.count()}"
        )

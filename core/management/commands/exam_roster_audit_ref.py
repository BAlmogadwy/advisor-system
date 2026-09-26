"""Print the audit reference of one or more student IDs (Student lists lookups).

A Student lists lookup is audited without a student ID, a name or the search
text: the row names the student asked for, and each student shown, by a keyed
reference (`core.services.exam_roster_view.audit_subject_ref`, an HMAC under
the site's secret). To answer "who looked up student X", compute X's reference
here and search the audit log for it.

Read-only. It writes nothing and reads no table; it prints the IDs it is given
beside their references, so run it where an auditor may see those IDs.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand

from core.services.exam_roster_view import audit_subject_ref


class Command(BaseCommand):
    help = "Print the audit reference that Student lists lookups record for each student ID."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("student_ids", nargs="+", type=int, metavar="STUDENT_ID")

    def handle(self, *args: Any, **options: Any) -> None:
        for student_id in options["student_ids"]:
            self.stdout.write(f"{student_id} {audit_subject_ref(student_id)}")

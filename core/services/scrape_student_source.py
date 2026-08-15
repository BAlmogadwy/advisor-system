"""Build the explicit, reviewed roster eligible for portal scraping.

Database mode never means every row literally: terminal/inactive records,
unknown future statuses, non-production programmes, and fixture identifiers stay
in the local database but are excluded from university-portal traffic.
"""

from __future__ import annotations

import hashlib
import re
from typing import TypedDict

from core.models import Student


class ScrapeStudentRow(TypedDict):
    student_id: str
    program: str
    section: str


class DatabaseStudentSourceSummary(TypedDict):
    total: int
    valid: int
    excluded: int
    invalid: int
    ready: bool
    roster_sha256: str
    excluded_reasons: dict[str, int]


PORTAL_STUDENT_ID_PATTERN = re.compile(r"[0-9]{7}")
PORTAL_PROGRAMS = frozenset(
    {"AI", "AI2", "COE", "COE2", "CS", "CS2", "CYP", "CYP2", "DS", "DS2", "IS", "IS2"}
)
PORTAL_STUDENT_STATUSES = frozenset(
    {
        "ACTIVE",
        "ACTIVE WITH ACADEMIC WARNING 1",
        "ACTIVE WITH ACADEMIC WARNING 2",
        "GRADUATION EXPECTED",
        "FAIL IN LAST TERM",
        "VISITOR TO ANOTHER UNIVERSITY",
    }
)


def _normalise_label(value: object) -> str:
    return " ".join(str(value or "").split()).upper()


def _normalise_database_student(
    student_id: object,
    program: object,
    section: object,
) -> ScrapeStudentRow | None:
    sid = str(student_id).strip()
    normalised_program = _normalise_label(program)
    normalised_section = _normalise_label(section)
    if (
        PORTAL_STUDENT_ID_PATTERN.fullmatch(sid) is None
        or not normalised_program
        or normalised_section not in {"M", "F"}
    ):
        return None
    return {
        "student_id": sid,
        "program": normalised_program,
        "section": normalised_section,
    }


def _database_student_rows() -> list[tuple[object, object, object, object]]:
    return list(
        Student.objects.order_by("student_id").values_list(
            "student_id",
            "program",
            "section",
            "status",
        )
    )


def _roster_sha256(students: list[ScrapeStudentRow]) -> str:
    digest = hashlib.sha256()
    for student in students:
        digest.update(student["student_id"].encode("ascii"))
        digest.update(b"\t")
        digest.update(student["program"].encode("utf-8"))
        digest.update(b"\t")
        digest.update(student["section"].encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _classify_database_students(
    rows: list[tuple[object, object, object, object]],
) -> tuple[list[ScrapeStudentRow], dict[str, int], int]:
    students: list[ScrapeStudentRow] = []
    excluded_reasons: dict[str, int] = {}
    invalid = 0
    for student_id, program, section, status in rows:
        sid = str(student_id).strip()
        if PORTAL_STUDENT_ID_PATTERN.fullmatch(sid) is None:
            # Local fixtures and malformed identifiers are not valid portal
            # identities. Keep them in the database, but never send them to the
            # university portal as part of an all-roster scrape.
            excluded_reasons["non_portal_student_id"] = (
                excluded_reasons.get("non_portal_student_id", 0) + 1
            )
            continue
        normalised_status = _normalise_label(status)
        if normalised_status not in PORTAL_STUDENT_STATUSES:
            excluded_reasons["status_not_in_scope"] = (
                excluded_reasons.get("status_not_in_scope", 0) + 1
            )
            continue
        normalised_program = _normalise_label(program)
        if not normalised_program:
            invalid += 1
            continue
        if normalised_program not in PORTAL_PROGRAMS:
            excluded_reasons["programme_not_in_scope"] = (
                excluded_reasons.get("programme_not_in_scope", 0) + 1
            )
            continue
        student = _normalise_database_student(student_id, program, section)
        if student is None:
            invalid += 1
            continue
        students.append(student)
    return students, excluded_reasons, invalid


def inspect_database_student_source() -> DatabaseStudentSourceSummary:
    rows = _database_student_rows()
    students, excluded_reasons, invalid = _classify_database_students(rows)
    excluded = sum(excluded_reasons.values())
    return {
        "total": len(rows),
        "valid": len(students),
        "excluded": excluded,
        "invalid": invalid,
        "ready": bool(students) and invalid == 0,
        "roster_sha256": _roster_sha256(students),
        "excluded_reasons": excluded_reasons,
    }


def load_database_students(
    *,
    expected_count: int | None = None,
    expected_roster_sha256: str = "",
) -> list[ScrapeStudentRow]:
    rows = _database_student_rows()
    students, _excluded_reasons, invalid = _classify_database_students(rows)

    if not rows:
        raise RuntimeError("The database contains no students to scrape.")
    if not students:
        raise RuntimeError("The database contains no eligible seven-digit portal students.")
    if invalid:
        raise RuntimeError(
            "Database student source is not ready: "
            f"{invalid} eligible student record(s) have a missing programme or invalid section."
        )
    actual_sha256 = _roster_sha256(students)
    if expected_count is not None and len(students) != expected_count:
        raise RuntimeError(
            "Database student roster changed after start approval; refusing to scrape."
        )
    if expected_roster_sha256 and actual_sha256 != expected_roster_sha256:
        raise RuntimeError(
            "Database student roster changed after start approval; refusing to scrape."
        )
    return students

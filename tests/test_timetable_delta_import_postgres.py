# mypy: disable-error-code="no-untyped-def"

"""PostgreSQL-only coverage for timetable-delta row locking.

SQLite erases ``select_for_update()``, so it cannot detect either PostgreSQL's
``FOR UPDATE``/``DISTINCT`` incompatibility or a missing touched-student lock.
The narrow PostgreSQL CI job runs this file with ``REQUIRE_POSTGRES_TESTS=1``.
"""

from __future__ import annotations

import os
import threading

import pytest
from django.db import DatabaseError, connection, connections, transaction

from core.models import Student, StudentTermSection, TermSection
from core.services import timetable_delta_import as importer

pytestmark = pytest.mark.django_db(transaction=True)


def _require_postgresql() -> None:
    if connection.vendor == "postgresql":
        return
    if os.environ.get("REQUIRE_POSTGRES_TESTS") == "1":
        pytest.fail(
            "PostgreSQL timetable-delta tests were required, but Django is using "
            f"{connection.vendor!r}."
        )
    pytest.skip("Requires PostgreSQL row-lock semantics.")


@pytest.fixture(autouse=True)
def _postgresql_only():
    _require_postgresql()


def _section(number: str, name: str) -> TermSection:
    return TermSection.objects.create(
        scenario=None,
        source_tag="scraper_timetable",
        course_code="AI",
        course_number=number,
        course_key=f"AI{number}",
        course_name=f"AI{number}",
        section=name,
    )


def test_lock_path_dedupes_linked_students_and_locks_zero_base_touched_student():
    linked_student = Student.objects.create(
        student_id=9901,
        program="AI",
        section="M",
        status="ACTIVE",
    )
    zero_base_student = Student.objects.create(
        student_id=9999,
        program="AI",
        section="M",
        status="ACTIVE",
    )
    for section in (_section("901", "M1"), _section("902", "M2")):
        StudentTermSection.objects.create(
            student_id=linked_student.student_id,
            academic_year="1448",
            term="1",
            source="scraper_timetable",
            term_section=section,
        )

    row_lock_outcome: list[BaseException | None] = []
    table_lock_outcome: list[BaseException | None] = []

    def contend_for_student_locks() -> None:
        try:
            with transaction.atomic():
                list(
                    Student.objects.select_for_update(nowait=True)
                    .filter(student_id=zero_base_student.student_id)
                    .order_by("student_id")
                )
        except BaseException as exc:  # noqa: BLE001 - the database outcome is asserted below
            row_lock_outcome.append(exc)
        else:
            row_lock_outcome.append(None)
        finally:
            connections["default"].close()

    def contend_with_phantom_insert() -> None:
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SELECT set_config('lock_timeout', %s, true)", ["1s"])
                Student.objects.create(
                    student_id=9998,
                    program="AI",
                    section="M",
                    status="ACTIVE",
                )
        except BaseException as exc:  # noqa: BLE001 - the database outcome is asserted below
            table_lock_outcome.append(exc)
        else:
            table_lock_outcome.append(None)
        finally:
            connections["default"].close()

    with transaction.atomic():
        importer._take_database_import_lock()
        # This call itself regresses the former SELECT DISTINCT ... FOR UPDATE
        # syntax failure caused by trying to deduplicate linked student IDs in SQL.
        importer._lock_current_timetable_state([zero_base_student.student_id])
        worker = threading.Thread(target=contend_for_student_locks, daemon=True)
        worker.start()
        worker.join(timeout=15)
        assert not worker.is_alive(), "row-lock contention probe did not finish"
        assert len(row_lock_outcome) == 1
        assert isinstance(row_lock_outcome[0], DatabaseError)

        insert_worker = threading.Thread(target=contend_with_phantom_insert, daemon=True)
        insert_worker.start()
        insert_worker.join(timeout=15)
        assert not insert_worker.is_alive(), "table-lock contention probe did not finish"
        assert len(table_lock_outcome) == 1
        assert isinstance(table_lock_outcome[0], DatabaseError)

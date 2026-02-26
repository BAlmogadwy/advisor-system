"""
Data migration: remove duplicate rows before adding unique constraints.

- StudentCourse: keeps the row with the highest id for each (student_id, course_id).
- Prerequisite: keeps the row with the highest id for each
  (program, course_code, prerequisite_course_code).
"""

from typing import Any

from django.db import migrations


def _dedup_student_courses(apps: Any, schema_editor: Any) -> None:
    """Delete duplicate StudentCourse rows, keeping the one with the highest id."""
    StudentCourse = apps.get_model("core", "StudentCourse")
    db_alias = schema_editor.connection.alias

    # Find (student_id, course_id) pairs that have more than one row.
    from django.db.models import Count, Max

    dupes = (
        StudentCourse.objects.using(db_alias)
        .values("student_id", "course_id")
        .annotate(cnt=Count("id"), max_id=Max("id"))
        .filter(cnt__gt=1)
    )
    total_deleted = 0
    for row in dupes:
        deleted, _ = (
            StudentCourse.objects.using(db_alias)
            .filter(
                student_id=row["student_id"],
                course_id=row["course_id"],
            )
            .exclude(id=row["max_id"])
            .delete()
        )
        total_deleted += deleted
    if total_deleted:
        print(f"\n  Removed {total_deleted} duplicate StudentCourse rows.")


def _dedup_prerequisites(apps: Any, schema_editor: Any) -> None:
    """Delete duplicate Prerequisite rows, keeping the one with the highest id."""
    Prerequisite = apps.get_model("core", "Prerequisite")
    db_alias = schema_editor.connection.alias

    from django.db.models import Count, Max

    dupes = (
        Prerequisite.objects.using(db_alias)
        .values("program", "course_code", "prerequisite_course_code")
        .annotate(cnt=Count("id"), max_id=Max("id"))
        .filter(cnt__gt=1)
    )
    total_deleted = 0
    for row in dupes:
        deleted, _ = (
            Prerequisite.objects.using(db_alias)
            .filter(
                program=row["program"],
                course_code=row["course_code"],
                prerequisite_course_code=row["prerequisite_course_code"],
            )
            .exclude(id=row["max_id"])
            .delete()
        )
        total_deleted += deleted
    if total_deleted:
        print(f"\n  Removed {total_deleted} duplicate Prerequisite rows.")


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0008_add_sc_student_status_index"),
    ]

    operations = [
        migrations.RunPython(
            _dedup_student_courses,
            migrations.RunPython.noop,
            elidable=True,
        ),
        migrations.RunPython(
            _dedup_prerequisites,
            migrations.RunPython.noop,
            elidable=True,
        ),
    ]

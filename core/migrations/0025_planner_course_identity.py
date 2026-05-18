from django.db import migrations, models


def _normalize_code(code):
    return str(code or "").replace("\u00a0", " ").strip().upper().replace(" ", "")


def backfill_planner_course_identity(apps, schema_editor):
    Course = apps.get_model("core", "Course")
    ScenarioSectionBudget = apps.get_model("core", "ScenarioSectionBudget")
    ScenarioStudentMap = apps.get_model("core", "ScenarioStudentMap")
    db_alias = schema_editor.connection.alias

    course_names = {
        _normalize_code(code): description or ""
        for code, description in Course.objects.using(db_alias).values_list(
            "course_code", "description"
        )
    }

    for budget in ScenarioSectionBudget.objects.using(db_alias).all().iterator():
        changed = []
        if not budget.course_key:
            budget.course_key = _normalize_code(budget.course_code)
            changed.append("course_key")
        if not budget.course_name:
            budget.course_name = course_names.get(_normalize_code(budget.course_code), "")
            changed.append("course_name")
        if changed:
            budget.save(update_fields=changed)

    for student_map in ScenarioStudentMap.objects.using(db_alias).all().iterator():
        if not student_map.recommended_course_keys:
            student_map.recommended_course_keys = list(student_map.recommended_courses or [])
            student_map.save(update_fields=["recommended_course_keys"])


def reverse_planner_course_identity(apps, schema_editor):
    # Keep rollback non-destructive; reversing drops the added columns.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0024_programmerequirement_course_name"),
    ]

    operations = [
        migrations.AddField(
            model_name="scenariostudentmap",
            name="recommended_course_keys",
            field=models.JSONField(default=list),
        ),
        migrations.AddField(
            model_name="scenariosectionbudget",
            name="course_key",
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="scenariosectionbudget",
            name="course_name",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.RunPython(backfill_planner_course_identity, reverse_planner_course_identity),
        migrations.RemoveConstraint(
            model_name="scenariosectionbudget",
            name="ux_ssb_scenario_course",
        ),
        migrations.AddIndex(
            model_name="scenariosectionbudget",
            index=models.Index(fields=["scenario", "course_key"], name="idx_ssb_scenario_key"),
        ),
        migrations.AddConstraint(
            model_name="scenariosectionbudget",
            constraint=models.UniqueConstraint(
                condition=models.Q(course_key__isnull=False) & ~models.Q(course_key=""),
                fields=("scenario", "course_key"),
                name="ux_ssb_scenario_course_key",
            ),
        ),
    ]

"""A stored, immutable case reference plus a student-visible resolution.

The reference is added in three steps rather than one because a unique column
cannot simply appear on a populated table: the field arrives blank, every existing
row is given a number, and only then is uniqueness enforced. The table is empty
today — which is exactly why this is the moment to do it properly.
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def number_existing_cases(apps, schema_editor):
    """Give any pre-existing case a reference, oldest first.

    Allocated here rather than by the model's own save(): a data migration must not
    depend on application code that may have moved on by the time it runs.
    """
    Escalation = apps.get_model("core", "AdvisorEscalation")
    Counter = apps.get_model("core", "AdvisorReferenceCounter")
    for case in Escalation.objects.order_by("created_at").iterator():
        year = case.created_at.year
        counter, _ = Counter.objects.get_or_create(year=year)
        counter.last_number += 1
        counter.save(update_fields=["last_number"])
        case.reference = f"ADV-{year}-{counter.last_number:05d}"
        case.save(update_fields=["reference"])


def drop_references(apps, schema_editor):
    apps.get_model("core", "AdvisorEscalation").objects.update(reference="")


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0048_advisor_escalation_reason_vocabulary"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="AdvisorReferenceCounter",
            fields=[
                ("year", models.PositiveIntegerField(primary_key=True, serialize=False)),
                ("last_number", models.PositiveIntegerField(default=0)),
            ],
            options={"db_table": "advisor_reference_counters"},
        ),
        migrations.AddField(
            model_name="advisorescalation",
            name="reference",
            field=models.CharField(default="", editable=False, max_length=24),
            preserve_default=False,
        ),
        migrations.RunPython(number_existing_cases, drop_references),
        migrations.AlterField(
            model_name="advisorescalation",
            name="reference",
            field=models.CharField(editable=False, max_length=24, unique=True),
        ),
        migrations.AddField(
            model_name="advisorescalation",
            name="resolution_message",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="advisorescalation",
            name="resolved_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="advisor_escalations_resolved",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]

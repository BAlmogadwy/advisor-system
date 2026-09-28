"""A room written without exam seats starts at its capacity.

Nothing in the database changes: ``exam_capacity`` stays a NOT NULL integer
column and every row keeps its value. Only the field's Python side changes -
its default is "not given" (``None``), which ``ExamCapacityField.pre_save``
turns into the room's capacity when the row is written. So the migration
alters the model state alone: an AlterField would make SQLite rebuild the
rooms table to change nothing.
"""

from django.db import migrations

import core.models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0073_rooms_1448t2_women_campus"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AlterField(
                    model_name="room",
                    name="exam_capacity",
                    field=core.models.ExamCapacityField(blank=True, default=None),
                ),
            ],
            database_operations=[],
        ),
    ]

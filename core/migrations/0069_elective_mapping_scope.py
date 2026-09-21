from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0068_section_instructor_constraints")]

    operations = [
        migrations.CreateModel(
            name="ElectiveMappingScope",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("academic_year", models.TextField()),
                ("term", models.PositiveSmallIntegerField()),
                ("programme", models.TextField()),
            ],
            options={
                "db_table": "elective_mapping_scopes",
                "constraints": [
                    models.UniqueConstraint(
                        fields=("academic_year", "term", "programme"),
                        name="uq_elective_mapping_scope",
                    )
                ],
            },
        )
    ]

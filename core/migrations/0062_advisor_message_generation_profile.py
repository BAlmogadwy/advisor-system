from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0061_scope_student_term_section_uniqueness"),
    ]

    operations = [
        migrations.AddField(
            model_name="advisormessage",
            name="generation_profile",
            field=models.CharField(
                blank=True,
                default="",
                editable=False,
                max_length=32,
            ),
        ),
    ]

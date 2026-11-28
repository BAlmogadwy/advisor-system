"""The registrar's exempt grade (E, and its Arabic form) settles a plan row: it is met.

Until now E sat with the grades that carry no outcome (IP, W, IC), so an exempted row stayed
``not_taken``: the planner and the recommender offered the course again (ENG101 to a student
who had passed ENG102 with an A) and the app's own ledger was short of the registrar's earned
hours by exactly the exempted hours. ``parse_course_result`` now settles E like TRS; this
brings the rows already stored to what the next scrape would write.

Only the status moves. The grade and mark stay as the registrar gave them. Not reversible:
which of these rows were stored ``passed`` before cannot be told apart afterwards.
"""

from django.db import migrations

EXEMPT_GRADES = ("E", "\u0639\u0641")


def settle_exempt_rows(apps, schema_editor):
    StudentCourse = apps.get_model("core", "StudentCourse")
    StudentCourse.objects.filter(grade__in=EXEMPT_GRADES).exclude(status="passed").update(
        status="passed"
    )


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0085_clean_course_title_number_debris"),
    ]

    operations = [
        migrations.RunPython(settle_exempt_rows, migrations.RunPython.noop),
    ]

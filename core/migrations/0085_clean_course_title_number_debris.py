"""Clean the course names an Oracle plan import stored with its number in front.

Six courses carried scrape debris in their English name: ``')\\t203\\t CALCULUS I'``
(MATH203), ``')\\t243\\t LINEAR ALGEBRA I'`` (MATH243), ``'345\\t WEB APPLICATION
DEVELOPMENT II'`` (IS345), ``')\\t224\\t COMPUTER ORGANIZATION AND ARCHITECTURE I'``
(COE224), ``'388\\t GRADUATION PROJECT I'`` (CYB388) and ``')\\t321\\t COMPUTER
ORGANIZATION & ARCHITECTURE II'`` (COE321), in ``Course.description`` and in the
``course_name`` of the plan rows that list them (20 rows of 12 programmes).

Where it came from: ``oracle_plan_parser.COURSE_PATTERN`` matched the number
between the Arabic and the English name as ANY number, and its lazy Arabic-name
group stopped at the first number inside the Arabic name ("تطوير تطبيقات الويب 2",
"تفاضل وتكامل ( 1 )"). The English name then began with the rest of the line: a
stray ``)``, a tab, the course's own number, a tab. ``import_oracle_plan_from_rows``
writes that name to both tables. The parser now matches the course's own number
there (``\\3``), so a new import is clean; this migration cleans what is stored.

EXACTLY that debris and nothing else: an optional ``)``, spaces or tabs, the
course's OWN three-digit number (the last three digits of its code), a tab, then
the name. Anything else in a name is left as it is. Running it again finds nothing.
The reverse does nothing: the debris is not worth restoring.

Saved scenarios (``scenario_id`` set) are not touched: their sections carry the name
inside their ``course_key`` (``MATH243::243_LINEAR_ALGEBRA_I``), which is their
identity, and saved scenarios are disposable test data.

PRODUCTION does not run ``migrate`` on start (the image's command is gunicorn
only): a deploy of this change must run ``python manage.py migrate`` in the
container.
"""

import re

from django.db import migrations

#: An optional ")", spaces or tabs, three digits, a tab: the start of every damaged name.
DEBRIS = re.compile(r"^\)?[ \t]*(\d{3})\t[ \t]*(?=\S)")
OWN_NUMBER = re.compile(r"(\d{3})$")


def clean_name(name: object, course_code: object) -> str | None:
    """The name without the debris, or None when the name does not carry it.

    Inlined here (no service import): a migration must keep doing what it did the
    day it was written.
    """
    text = str(name or "")
    code = str(course_code or "").replace(" ", "").upper()
    own = OWN_NUMBER.search(code)
    found = DEBRIS.match(text)
    if not own or not found or found.group(1) != own.group(1):
        return None
    cleaned = text[found.end() :].strip()
    return cleaned or None


def forwards(apps, schema_editor):
    Course = apps.get_model("core", "Course")
    ProgrammeRequirement = apps.get_model("core", "ProgrammeRequirement")
    for model, field in ((Course, "description"), (ProgrammeRequirement, "course_name")):
        rows = model.objects.filter(**{f"{field}__contains": "\t"}).values_list(
            "pk", "course_code", field
        )
        for pk, course_code, name in list(rows):
            cleaned = clean_name(name, course_code)
            if cleaned is not None:
                model.objects.filter(pk=pk).update(**{field: cleaned})


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0084_course_instructor_max_sections"),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]

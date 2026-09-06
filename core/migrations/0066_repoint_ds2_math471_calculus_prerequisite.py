"""Repoint DS2/MATH471's Calculus II prerequisite from MATH204 to MATH106.

The registrar's DS2 plan document lists MATH471 (Optimization & Modeling for
Computing) with prerequisites MATH204 and MATH243, and the database imported that
faithfully.  The document is what is wrong: MATH204 belongs to the FIRST-cohort
numbering (MATH203 Calculus I / MATH204 Calculus II).  The second-cohort plans
renumbered the same courses (MATH105 Calculus I / MATH106 Calculus II), and
MATH204 appears nowhere in the DS2 plan.

Evidence that MATH106 is the course meant:

* DS2's maths spine is MATH105 -> MATH106 -> MATH243 -> MATH471; there is no
  MATH204 requirement row to satisfy.
* DS2 already resolves Calculus II correctly for its other dependant:
  ``STAT307 -> MATH106`` is the exact second-cohort analogue of DS's
  ``STAT301 -> MATH204``.  MATH471 was the sole holdout.
* 191 DS2 students have a MATH106 record (37 passed, 81 studying, 73 not taken).
  ZERO have any MATH204 record at all.

Repointed rather than deleted.  MATH471 genuinely requires Calculus II; deleting
the row produces an identical forecast while silently dropping a real academic
prerequisite, which hides the mistake instead of correcting it.

Effect: DS2 students with a graduation forecast go from 0/191 to 177/191.  With
MATH204 unsatisfiable, MATH471 was permanently blocked, and its three credits put
every student under the zero-slack ``147(HOURS)`` gate on the co-op course DS492,
leaving two unresolved requirements and no estimate.
"""

from django.db import migrations

PROGRAM = "DS2"
COURSE_CODE = "MATH471"
WRONG_PREREQUISITE = "MATH204"
CORRECT_PREREQUISITE = "MATH106"


def _repoint(apps, schema_editor, *, old: str, new: str) -> None:
    """Move the cell from `old` to `new`, keyed on the natural key.

    Keyed on (program, course_code, prerequisite_course_code) rather than the
    primary key: ids are not stable across the local database and a rebuilt
    online one, so `id=742` names a different row -- or nothing -- there.

    A unique constraint covers exactly that natural key, so if the target row
    already exists (a partial re-application, or a hand-edit through the DB admin
    screen) an UPDATE would violate it.  Drop the stale row in that case; the
    intended end state is the same either way.
    """

    prerequisite = apps.get_model("core", "Prerequisite")
    stale = prerequisite.objects.filter(
        program=PROGRAM, course_code=COURSE_CODE, prerequisite_course_code=old
    )
    if not stale.exists():
        return
    already_correct = prerequisite.objects.filter(
        program=PROGRAM, course_code=COURSE_CODE, prerequisite_course_code=new
    ).exists()
    if already_correct:
        stale.delete()
        return
    stale.update(prerequisite_course_code=new)


def forwards(apps, schema_editor) -> None:
    _repoint(apps, schema_editor, old=WRONG_PREREQUISITE, new=CORRECT_PREREQUISITE)


def backwards(apps, schema_editor) -> None:
    _repoint(apps, schema_editor, old=CORRECT_PREREQUISITE, new=WRONG_PREREQUISITE)


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0065_advisormessage_evidence_audit"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]

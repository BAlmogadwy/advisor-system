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

SCOPE: this repairs an EXISTING database; it is not production protection.  A
rebuilt online database migrates against empty curriculum tables (migration 0002
seeds no prerequisite rows without a legacy source), and its data then arrives
wholesale from ``import_release_seed``, which flushes the target and loads a seed
exported from the local database -- ``core.prerequisite`` is in that seed's model
list.  So the corrected row reaches production only because the local database is
corrected here first.  A seed captured from an UNFIXED database would reintroduce
MATH204, and this migration, already applied, would not run again.  The check in
``core.services.curriculum_integrity`` (surfaced by ``run_integrity_checks``) is
what catches that.
"""

import logging

from django.db import migrations

logger = logging.getLogger(__name__)

PROGRAM = "DS2"
COURSE_CODE = "MATH471"
WRONG_PREREQUISITE = "MATH204"
CORRECT_PREREQUISITE = "MATH106"


def _normalise(value: object) -> str:
    """The project's `normalize_code`, inlined.

    A migration must keep behaving the way it did the day it was written, so it
    does not import a service function that is free to change under it.
    """

    return str(value or "").replace(chr(0xA0), " ").strip().upper().replace(" ", "")


def _repoint(apps, schema_editor, *, old: str, new: str) -> None:
    """Move the cell from `old` to `new`, keyed on the natural key.

    Keyed on (program, course_code, prerequisite_course_code) rather than the
    primary key: ids are not stable across the local database and a rebuilt
    online one, so `id=742` names a different row -- or nothing -- there.

    Matched on NORMALISED values rather than a database-side `program="DS2"`.
    Both engines compare `=` case- and whitespace-sensitively, so an exact filter
    would silently skip a row stored as "ds2" or with a stray space -- and a
    silent no-op here leaves 191 students broken while reporting success.  The
    sibling check in `core.services.curriculum_integrity` normalises for exactly
    this reason; the two halves of one change should not hold opposite positions
    on the same risk.  The table is small enough to scan.

    A unique constraint covers the natural key, so if the target row already
    exists (a partial re-application, or a hand-edit through the DB admin screen)
    an UPDATE would violate it.  Drop the stale row in that case; the intended end
    state is the same either way.
    """

    prerequisite = apps.get_model("core", "Prerequisite")
    rows = list(
        prerequisite.objects.values_list(
            "id", "program", "course_code", "prerequisite_course_code"
        )
    )
    stale_ids = [
        row_id
        for row_id, program, course_code, cell in rows
        if _normalise(program) == PROGRAM
        and _normalise(course_code) == COURSE_CODE
        and _normalise(cell) == old
    ]
    if not stale_ids:
        logger.info(
            "0066: no %s/%s -> %s row to repoint (%d prerequisite rows in total). "
            "Expected on a fresh database, where the curriculum arrives later from "
            "the release seed.",
            PROGRAM,
            COURSE_CODE,
            old,
            len(rows),
        )
        return

    already_correct = any(
        _normalise(program) == PROGRAM
        and _normalise(course_code) == COURSE_CODE
        and _normalise(cell) == new
        for _row_id, program, course_code, cell in rows
    )
    if already_correct:
        prerequisite.objects.filter(id__in=stale_ids).delete()
        return
    prerequisite.objects.filter(id__in=stale_ids).update(prerequisite_course_code=new)


def forwards(apps, schema_editor) -> None:
    _repoint(apps, schema_editor, old=WRONG_PREREQUISITE, new=CORRECT_PREREQUISITE)


def backwards(apps, schema_editor) -> None:
    """Reversible, but deliberately does NOT restore MATH204.

    The obvious mirror -- repoint MATH106 back to MATH204 -- is unconditional, and
    would therefore CREATE the defect on any database that never had it.  A
    rebuilt online database is exactly that case: this migration no-ops against
    its empty curriculum tables, the corrected row then arrives via
    `import_release_seed`, and a later rollback to 0065 would silently break all
    191 DS2 forecasts on a database that was correct a moment earlier.

    MATH204 is not a prior state worth restoring -- it is a transcription error
    that no DS2 student can satisfy.  Rolling this migration back unwinds the
    schema position and leaves the data correct, which is the only safe reading of
    "reverse a data correction".
    """
    logger.info(
        "0066 reversed: leaving %s/%s -> %s in place. Reversing a data correction "
        "must not re-introduce the error it fixed.",
        PROGRAM,
        COURSE_CODE,
        CORRECT_PREREQUISITE,
    )


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0065_advisormessage_evidence_audit"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]

# mypy: disable-error-code="no-untyped-def"

"""PostgreSQL only: two writes to one programme's study plan at once (PR 2).

A study-plan import locks the rows it may rewrite (``_lock_plan_rows``), but a
row lock holds only the rows that exist when its statement starts. A second
replace of the same programme, waiting on those rows, used to get none of the
rows the first one created; its next statement then deleted them, so the
first import's weekly-meetings votes and course names were lost without a
word, and a catalogue delete waiting the same way listed none of the votes it
deleted. Each catalogue write now takes its programmes' catalogue lock first
(``_lock_programme_catalogues``), so the second reads the programme only once
the first has committed.

The other guard is the row lock itself: a Section Planning save of a row the
import holds waits for the import, then finds the row gone and is refused
(``PatternConflict``) instead of saving a vote the import deletes unseen.

SQLite runs one writer at a time (BEGIN IMMEDIATE) and erases
``select_for_update()``, so this runs on PostgreSQL only; the narrow
``planner (PostgreSQL 16)`` CI job runs it with ``REQUIRE_POSTGRES_TESTS=1``,
where a skip is a failure.

The interleaving is forced, not hoped for. Both connections are opened before
either write starts. The first write stops inside its transaction, after its
writes (or after its row lock, for the Section Planning case), and holds the
transaction open until PostgreSQL shows the second write waiting for a lock
(``pg_locks``, read live, never a cached statistics view), the second write
has finished, or ``HOLD_SECONDS`` have passed. Only then does the first
commit. The catalogue tests also assert that the second write did wait, so a
run that did not force the interleaving fails instead of passing.
"""

from __future__ import annotations

import os
import threading
import time

import pytest
from django.db import connection, connections

from core.models import ProgrammeRequirement
from core.services import db_admin_ops
from core.services import programme_meeting_pattern_editor as editor

pytestmark = pytest.mark.django_db(transaction=True)

#: The longest the first write holds its transaction open for the second.
HOLD_SECONDS = 10
#: How often the first write looks for the second one waiting.
POLL_SECONDS = 0.02

STAT = "PRINCIPLES OF STATISTICS"
ALGEBRA = "LINEAR ALGEBRA I"
PROGRAMMING = "FUNDAMENTALS OF PROGRAMMING"

#: The first write: a replace that keeps STAT103 and MATH243 and drops CS111.
FIRST_CSV = (
    "course_code,programme_term,credit_hours,type\nSTAT103,1,3,Mandatory\nMATH243,3,3,Mandatory\n"
)
#: Both votes, as each write that keeps them reports them.
KEPT = [
    {
        "program": "AI2",
        "course_code": "MATH243",
        "course_key": "MATH243::LINEAR_ALGEBRA_I",
        "meeting_pattern": "standard",
    },
    {
        "program": "AI2",
        "course_code": "STAT103",
        "course_key": "STAT103::PRINCIPLES_OF_STATISTICS",
        "meeting_pattern": "2x100",
    },
]


def _require_postgresql() -> None:
    if connection.vendor == "postgresql":
        return
    if os.environ.get("REQUIRE_POSTGRES_TESTS") == "1":
        pytest.fail(
            "PostgreSQL catalogue-lock tests were required, but Django is using "
            f"{connection.vendor!r}."
        )
    pytest.skip("Requires PostgreSQL row-lock and advisory-lock semantics.")


@pytest.fixture(autouse=True)
def _postgresql_only():
    _require_postgresql()


def _ai2_plan():
    """AI2's plan: two 3-credit courses with a vote, and one without."""
    rows = {}
    for code, name, vote, term in (
        ("STAT103", STAT, "2x100", 1),
        ("MATH243", ALGEBRA, "standard", 3),
        ("CS111", PROGRAMMING, "", 1),
    ):
        rows[code] = ProgrammeRequirement.objects.create(
            program="AI2",
            course_code=code,
            course_name=name,
            type="Mandatory",
            programme_term=term,
            credit_hours=3,
            meeting_pattern=vote,
        )
    return rows


def _plan(program="AI2"):
    return {
        row.course_code: (row.course_name, row.credit_hours, row.meeting_pattern)
        for row in ProgrammeRequirement.objects.filter(program=program)
    }


class _Hold:
    """Hold the thread named "first" until the "second" one queues behind it."""

    def __init__(self):
        self.first_in = threading.Event()
        self.second_done = threading.Event()
        self.second_pid: int | None = None
        self.second_waited = False
        self.resolved = False

    def until_second_queues(self):
        if threading.current_thread().name != "first":
            return
        self.first_in.set()
        deadline = time.monotonic() + HOLD_SECONDS
        with connection.cursor() as cursor:
            while time.monotonic() < deadline:
                if self.second_done.is_set():
                    self.resolved = True
                    return
                cursor.execute(
                    "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE pid = %s AND NOT granted)",
                    [self.second_pid],
                )
                if cursor.fetchone()[0]:
                    self.second_waited = self.resolved = True
                    return
                time.sleep(POLL_SECONDS)


def _hold_after(monkeypatch, name):
    """Stop the first write just after ``db_admin_ops.<name>`` returns."""
    hold = _Hold()
    original = getattr(db_admin_ops, name)

    def hooked(*args, **kwargs):
        result = original(*args, **kwargs)
        hold.until_second_queues()
        return result

    monkeypatch.setattr(db_admin_ops, name, hooked)
    return hold


def _race(hold, first, second):
    """Run ``first`` and ``second`` on two connections, the second once the first holds."""
    outcomes: dict[str, object] = {}
    warmed = threading.Barrier(2)

    def run(name, attempt):
        try:
            connection.ensure_connection()
            if name == "second":
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    hold.second_pid = cursor.fetchone()[0]
            warmed.wait(timeout=15)
            if name == "second" and not hold.first_in.wait(timeout=15):
                raise AssertionError("the first write never reached its hold")
            outcomes[name] = attempt()
        except BaseException as exc:  # noqa: BLE001 - the outcome is asserted below
            outcomes[name] = exc
        finally:
            if name == "second":
                hold.second_done.set()
            connections["default"].close()

    threads = [
        threading.Thread(target=run, name="first", args=("first", first), daemon=True),
        threading.Thread(target=run, name="second", args=("second", second), daemon=True),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
        assert not thread.is_alive(), "a write did not finish"
    assert hold.resolved, "the second write neither queued nor finished while the first held"
    return outcomes


def _first_replace():
    return db_admin_ops.import_program_plan("AI2", FIRST_CSV, replace_existing=True)


def _second_csv_replace(_tmp_path):
    return db_admin_ops.import_program_plan("AI2", FIRST_CSV, replace_existing=True)


def _second_oracle_replace(_tmp_path):
    rows = [
        {"code": code, "en_name": name, "credits": "3", "level_number": str(level)}
        for code, name, level in (("STAT103", STAT, 1), ("MATH243", ALGEBRA, 3))
    ]
    return db_admin_ops.import_oracle_plan_from_rows("AI2", rows, replace_existing=True)


def _second_legacy_load(tmp_path):
    requirements = tmp_path / "requirements.csv"
    requirements.write_text(
        "program,course_code,type,programme_term,credit_hours\nAI2,STAT103,Mandatory,1,4\n",
        encoding="utf-8",
    )
    prerequisites = tmp_path / "prerequisites.csv"
    prerequisites.write_text("program,course_code,prerequisite_course_code\n", encoding="utf-8")
    return db_admin_ops.legacy_load_department_files_exact(str(requirements), str(prerequisites))


def _second_catalogue_delete(_tmp_path):
    return db_admin_ops.delete_program_catalog("AI2")


@pytest.mark.parametrize(
    "second",
    [_second_csv_replace, _second_oracle_replace, _second_legacy_load, _second_catalogue_delete],
    ids=["csv-replace", "oracle-replace", "legacy-load", "catalogue-delete"],
)
def test_a_second_catalogue_write_reads_what_the_first_one_committed(monkeypatch, tmp_path, second):
    """The first replace commits STAT103 and MATH243, names and votes carried
    (CS111 left out). The second write, started while the first held its
    transaction open, must see exactly that: without the catalogue lock it
    read no rows at all and then rewrote or deleted the committed ones."""
    _ai2_plan()
    hold = _hold_after(monkeypatch, "_restore_votes")

    outcomes = _race(hold, _first_replace, lambda: second(tmp_path))

    assert hold.second_waited, "the second write never queued behind the first"
    first, result = outcomes["first"], outcomes["second"]
    assert not isinstance(first, BaseException), first
    assert first["meeting_patterns"] == {"kept": KEPT, "dropped": []}
    assert not isinstance(result, BaseException), result
    if second is _second_catalogue_delete:
        assert (result["requirements_count"], result["meeting_patterns_count"]) == (2, 2)
        assert result["deleted_meeting_patterns"] == [
            {key: vote[key] for key in ("course_code", "course_key", "meeting_pattern")}
            for vote in KEPT
        ]
        assert _plan() == {}
    elif second is _second_legacy_load:
        assert result["meeting_patterns"] == {
            "AI2": {
                "kept": [],
                "dropped": [
                    {
                        "program": "AI2",
                        "course_code": "STAT103",
                        "course_key": "STAT103::PRINCIPLES_OF_STATISTICS",
                        "old": "2x100",
                        "reason": "credits_changed",
                    }
                ],
            }
        }
        assert _plan() == {"STAT103": (STAT, 4, ""), "MATH243": (ALGEBRA, 3, "standard")}
    else:
        assert result["meeting_patterns"] == {"kept": KEPT, "dropped": []}
        assert _plan() == {"STAT103": (STAT, 3, "2x100"), "MATH243": (ALGEBRA, 3, "standard")}


def test_a_section_planning_save_during_a_replace_is_refused_not_lost(monkeypatch):
    """Section Planning sets CS111's vote while a replace holds AI2's rows. The
    save waits on the import's row lock, finds its row gone and is refused;
    without that lock it saved, and the import then deleted the vote unseen."""
    rows = _ai2_plan()
    hold = _hold_after(monkeypatch, "_lock_plan_rows")
    cs111 = rows["CS111"]
    write = editor.PlannedPatternWrite(
        requirement_id=cs111.pk,
        program="AI2",
        course_key="CS111::FUNDAMENTALS_OF_PROGRAMMING",
        course_code="CS111",
        course_name=PROGRAMMING,
        credit_hours=3,
        old="",
        new="2x100",
        scope="programme",
    )

    def save():
        editor.apply_pattern_writes([write], audit=lambda *_args: None)
        return "saved"

    replace_csv = FIRST_CSV + "CS111,1,3,Mandatory\n"
    outcomes = _race(
        hold,
        lambda: db_admin_ops.import_program_plan("AI2", replace_csv, replace_existing=True),
        save,
    )

    assert isinstance(outcomes["second"], editor.PatternConflict), outcomes
    first = outcomes["first"]
    assert not isinstance(first, BaseException), first
    assert first["meeting_patterns"] == {"kept": KEPT, "dropped": []}
    assert _plan() == {
        "STAT103": (STAT, 3, "2x100"),
        "MATH243": (ALGEBRA, 3, "standard"),
        "CS111": (PROGRAMMING, 3, ""),
    }

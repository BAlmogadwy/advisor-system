# mypy: disable-error-code="no-untyped-def,arg-type"
"""A programme's weekly-meetings vote through the release seed (PR 2, design §8).

The seed carries every ProgrammeRequirement field, so a vote set locally
reaches a production rebuild. A seed whose vote does not fit its row
(ck_pr_meeting_pattern_fits_credits) is refused by name in
``_validate_records``, before the target is flushed, not as a generic rollback
after it.
"""

from __future__ import annotations

import pytest
from django.core.management.base import CommandError

from core.models import AuditLog, ProgrammeRequirement, Student
from tests.test_export_release_seed_command import _export as _export_records
from tests.test_import_release_seed_command import (
    KILL_SWITCH_ENV,
    KILL_SWITCH_VALUE,
    _export,
    _replace,
    _rewrite_fixture,
    _source_records,
    _sqlite_rehearsal_target,
    _target_sentinel,
)

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def _strong_release_signing_key(monkeypatch):
    monkeypatch.setenv(
        "RELEASE_SEED_SIGNING_KEY",
        "9c4785723dc524554acd065e559452245d0ef08ee24d774ba1c93c15e1fb5db8",
    )


def _voted_row(vote="2x100") -> ProgrammeRequirement:
    return ProgrammeRequirement.objects.create(
        program="AI2",
        course_code="STAT103",
        course_name="PRINCIPLES OF STATISTICS",
        type="Mandatory",
        programme_term=1,
        credit_hours=3,
        meeting_pattern=vote,
    )


def test_a_programme_vote_leaves_with_the_release_seed(tmp_path):
    """The export twin of "programme marks never leave": a vote is department
    data, so it travels with its 3-credit row."""
    row = _voted_row()
    plain = ProgrammeRequirement.objects.create(
        program="AI2", course_code="CS111", course_name="FUNDAMENTALS", credit_hours=3
    )

    _, _, _, records = _export_records(tmp_path / "programme-votes-release.json.gz")

    exported = {
        record["pk"]: record["fields"]
        for record in records
        if record["model"] == "core.programmerequirement"
    }
    assert (exported[row.pk]["meeting_pattern"], exported[row.pk]["credit_hours"]) == ("2x100", 3)
    assert exported[plain.pk]["meeting_pattern"] == ""


def test_a_programme_vote_round_trips_into_the_target(tmp_path, monkeypatch):
    _source_records()
    row = _voted_row("standard")
    fixture, manifest = _export(tmp_path)
    _target_sentinel()
    # The target starts without the vote, so the loaded one came from the seed.
    ProgrammeRequirement.objects.filter(pk=row.pk).update(meeting_pattern="")
    monkeypatch.setenv(KILL_SWITCH_ENV, KILL_SWITCH_VALUE)

    with _sqlite_rehearsal_target(tmp_path / "target.sqlite3") as alias:
        _replace(fixture, manifest, database=alias)
        assert ProgrammeRequirement.objects.using(alias).get(pk=row.pk).meeting_pattern == (
            "standard"
        )


@pytest.mark.parametrize(
    ("vote", "credits", "reason"),
    [
        ("2x100", 4, "do not fit a programme requirement's credit hours"),
        ("2x100", None, "do not fit a programme requirement's credit hours"),
        ("standard", 4, "do not fit a programme requirement's credit hours"),
        ("2X100", 3, "unknown weekly meeting pattern"),
        (" 2x100", 3, "unknown weekly meeting pattern"),
        ("3x100", 3, "unknown weekly meeting pattern"),
    ],
)
def test_a_vote_that_does_not_fit_its_row_makes_no_writes(
    tmp_path, monkeypatch, vote, credits, reason
):
    """Refused by name before the flush: the target's own rows survive."""
    _source_records()
    row = _voted_row()
    fixture, manifest = _export(tmp_path)
    target = _target_sentinel()
    monkeypatch.setenv(KILL_SWITCH_ENV, KILL_SWITCH_VALUE)

    def tamper(records):
        rows = [
            r for r in records if r["model"] == "core.programmerequirement" and r["pk"] == row.pk
        ]
        assert [r["fields"]["meeting_pattern"] for r in rows] == ["2x100"]
        rows[0]["fields"].update(meeting_pattern=vote, credit_hours=credits)

    _rewrite_fixture(fixture, manifest, tamper)

    with _sqlite_rehearsal_target(tmp_path / "target.sqlite3") as alias:
        with pytest.raises(
            CommandError, match=f"Release seed refused: fixture contains .*{reason}"
        ):
            _replace(fixture, manifest, database=alias)
        assert Student.objects.using(alias).filter(pk=target.pk).exists()
        assert AuditLog.objects.using(alias).filter(action="target-sentinel").exists()

# mypy: disable-error-code="no-untyped-def"

from __future__ import annotations

from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from core.management.commands import import_timetable_delta
from core.management.commands.import_timetable_delta import _parse_expected_counts
from core.services.timetable_delta_import import EXPECTED_OPERATION_KEYS


def test_expected_count_parser_requires_supported_unique_nonnegative_keys():
    values = [f"{key}={index}" for index, key in enumerate(EXPECTED_OPERATION_KEYS)]
    assert _parse_expected_counts(values) == {
        key: index for index, key in enumerate(EXPECTED_OPERATION_KEYS)
    }

    with pytest.raises(CommandError, match="supported KEY=COUNT"):
        _parse_expected_counts(["accounts_deleted=1"])
    with pytest.raises(CommandError, match="Duplicate"):
        _parse_expected_counts(["sections_created=1", "sections_created=1"])
    with pytest.raises(CommandError, match="cannot be negative"):
        _parse_expected_counts(["sections_created=-1"])


def test_already_applied_dry_run_prints_zero_write_message(monkeypatch):
    monkeypatch.setattr(
        import_timetable_delta,
        "import_timetable_delta_artifact",
        lambda *args, **kwargs: {"mode": "already_applied", "writes_performed": False},
    )
    stdout = StringIO()
    call_command("import_timetable_delta", "artifact.json", stdout=stdout)
    output = stdout.getvalue()
    assert "TARGET ALREADY PRESENT" in output
    assert "zero writes were performed" in output
    assert "DRY RUN ONLY" not in output

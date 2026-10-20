"""Record the flag-OFF greedy golden that ``test_one_teacher_flag_parity`` replays.

Run it only at ``74a9a6c`` (the base the one-teacher feature starts from), never
on a branch that changes the planner: the golden must describe the code before
the feature, or P3 proves nothing. The steps are in that module's docstring::

    ONE_TEACHER_GOLDEN_RECORD=<file> python -m pytest -p no:cacheprovider \\
        tests/record_one_teacher_flag_off_golden.py

Not collected by default (no ``test_`` prefix); pytest runs it when named.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.test_one_teacher_flag_parity import build_corpus, flag_off, run_golden_build

__all__ = ["flag_off"]  # the settings fixture, imported for pytest to find


@pytest.mark.django_db
def test_record_flag_off_golden(flag_off) -> None:
    target = os.environ.get("ONE_TEACHER_GOLDEN_RECORD")
    if not target:
        pytest.fail("Set ONE_TEACHER_GOLDEN_RECORD to the file to write.", pytrace=False)
    produced = run_golden_build(build_corpus())
    Path(target).write_text(
        json.dumps(produced, indent=1, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

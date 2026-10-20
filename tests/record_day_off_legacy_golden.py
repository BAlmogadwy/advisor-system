"""Record the strict-relation golden that ``test_instructor_day_off_policy`` replays.

Run it against ``efad91a`` (the base the day-off policy starts from), never
against a branch that changes the policy::

    python tests/record_day_off_legacy_golden.py <efad91a checkout> <out.json>

It imports ``core.services.timetable_optimisation_policy`` from the checkout
given, replays ``tests/day_off_corpus.py`` (this branch's copy, which imports
only what the base already had) with instructor priority on and off, and writes
``{"priority_on": [...], "priority_off": [...]}``. Then diff ``<out.json>``
against ``tests/fixtures/day_off_legacy_policy_golden.json``.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


def main(base: str, target: str) -> None:
    sys.path.insert(0, str(Path(base).resolve()))
    from django.conf import settings

    settings.configure(TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED=True)
    from core.services import timetable_optimisation_policy as policy

    if not Path(policy.__file__).resolve().is_relative_to(Path(base).resolve()):
        raise SystemExit(f"imported {policy.__file__}, not the base under {base}")
    spec = importlib.util.spec_from_file_location(
        "day_off_corpus", Path(__file__).with_name("day_off_corpus.py")
    )
    corpus = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(corpus)
    produced = {}
    for name, priority in (("priority_on", True), ("priority_off", False)):
        settings.TIMETABLE_INSTRUCTOR_PRIORITY_ENABLED = priority
        produced[name] = corpus.legacy_outputs(policy, corpus.legacy_corpus())
    Path(target).write_text(json.dumps(produced, separators=(",", ":")) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main(*sys.argv[1:3])

"""``TIMETABLE_ONLINE_EVENING_WINDOWS``: online courses meet only in Online 1, 2 and 3.

Owner request 2026-10-26: online courses go only in the evening windows, Online 1
(15:50-17:30), Online 2 (17:40-19:20) and Online 3 (19:30-21:10), and nothing
else changes. It ships behind a flag that defaults OFF:

- **Flag OFF = baseline.** On a corpus WITH online courses, the greedy builds,
  the two CP-SAT models, the annealing / load-balancer relocate domains, the
  cap-repair slot domains and the one-teacher guard's options all reproduce a
  golden recorded on the UNCHANGED engine (``17ef51d``).
- **Flag ON without an online course = baseline.** On the corpus without online
  courses the same captures (and the day-spread golden's greedy builds and
  CP-SAT models) are unchanged.
- **Flag ON.** Every automatic placer gives an online section's meetings only
  the windows (whole 100-minute windows, any weekday) and never a daytime slot;
  other sections never get a window.

The golden lives in ``tests/fixtures/online_evening_windows_flag_off_golden.json``.
To re-check it, check out ``17ef51d`` plus the fixture commit and run::

    ONLINE_EVENING_GOLDEN_RECORD=<file> python -m pytest -p no:cacheprovider \\
        tests/test_online_evening_windows.py -k record

then diff ``<file>`` against the fixture. Never re-record it from a branch that
changes the planner.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests._online_windows_fixture import (
    GREEDY_CASES,
    ONLINE_FLAGS,
    build_online_corpus,
    capture_domains,
    run_greedy_case,
)
from tests.test_one_teacher_flag_parity import build_corpus
from tests.test_section_day_spread import _model_fingerprint, make_capture_models

GOLDEN = Path(__file__).parent / "fixtures" / "online_evening_windows_flag_off_golden.json"
RECORD_ENV = "ONLINE_EVENING_GOLDEN_RECORD"

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def flag_off(settings):
    for name, value in ONLINE_FLAGS.items():
        setattr(settings, name, value)
    settings.TIMETABLE_ONLINE_EVENING_WINDOWS = False
    return settings


@pytest.fixture
def capture_models(monkeypatch):
    return make_capture_models(monkeypatch)


def _case_key(strategy: str, seed: int | None) -> str:
    return f"{strategy}|{'unseeded' if seed is None else seed}"


# ── recorder (skipped unless asked) ───────────────────────────────────


def _merge_into_record(key: str, value) -> None:
    target = Path(os.environ[RECORD_ENV])
    data = json.loads(target.read_text(encoding="utf-8")) if target.exists() else {}
    data[key] = value
    target.write_text(
        json.dumps(data, indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8"
    )


@pytest.mark.skipif(not os.environ.get(RECORD_ENV), reason=f"set {RECORD_ENV} to record")
@pytest.mark.django_db
@pytest.mark.parametrize(("strategy", "seed"), GREEDY_CASES)
def test_record_online_greedy_golden(flag_off, strategy, seed) -> None:
    _merge_into_record(
        f"online:greedy:{_case_key(strategy, seed)}",
        run_greedy_case(build_online_corpus, strategy, seed),
    )


@pytest.mark.skipif(not os.environ.get(RECORD_ENV), reason=f"set {RECORD_ENV} to record")
@pytest.mark.django_db
def test_record_online_cpsat_golden(flag_off, capture_models) -> None:
    for name, model in capture_models(build_online_corpus(11)).items():
        _merge_into_record(f"online:cpsat:{name}", _model_fingerprint(model))


@pytest.mark.skipif(not os.environ.get(RECORD_ENV), reason=f"set {RECORD_ENV} to record")
@pytest.mark.django_db
def test_record_online_domains_golden(flag_off, monkeypatch) -> None:
    _merge_into_record("online:domains", capture_domains(monkeypatch, build_online_corpus(11)))


@pytest.mark.skipif(not os.environ.get(RECORD_ENV), reason=f"set {RECORD_ENV} to record")
@pytest.mark.django_db
def test_record_plain_domains_golden(flag_off, monkeypatch) -> None:
    _merge_into_record("plain:domains", capture_domains(monkeypatch, build_corpus(11)))

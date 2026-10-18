"""Per-run search randomness, isolated between planner worker threads.

The job boundary persists one root seed before execution. Each search stage
derives its own stable stream, so another stage consuming random numbers cannot
change a candidate's starting point. No process-global RNG is seeded here.
Legacy direct service calls outside a seeded run retain their existing defaults.
"""

from __future__ import annotations

import hashlib
import random
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

MAX_RUN_SEED = (1 << 31) - 1
_run_seed: ContextVar[int | None] = ContextVar("timetable_run_seed", default=None)


def parse_run_seed(value: object) -> int | None:
    """Accept an optional JSON integer in CP-SAT's nonnegative int32 range."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_RUN_SEED:
        raise ValueError(f"seed must be an integer between 0 and {MAX_RUN_SEED}")
    return value


def resolve_run_seed(value: object = None) -> int:
    """Draw a fresh seed, or retain an explicitly supplied replay seed."""
    supplied = parse_run_seed(value)
    return supplied if supplied is not None else secrets.randbelow(MAX_RUN_SEED + 1)


def current_run_seed() -> int | None:
    return _run_seed.get()


@contextmanager
def seeded_timetable_run(seed: int | None) -> Iterator[None]:
    """Bind a job's seed for its worker and restore the prior scope on exit."""
    token = _run_seed.set(parse_run_seed(seed))
    try:
        yield
    finally:
        _run_seed.reset(token)


def seed_for(scope: str, *, fallback: int = 42) -> int:
    """Derive a stable stage seed; use logical scope names, never database IDs.

    The fallback preserves legacy unseeded service behavior. Persisting a root
    seed records search starting points, not a byte-identical scheduling promise:
    wall-clock budgets and parallel solvers can still affect the final result.
    """
    root = current_run_seed()
    if root is None:
        return fallback
    digest = hashlib.blake2s(f"{root}:{scope}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big") & MAX_RUN_SEED


def random_for(scope: str) -> random.Random | None:
    """A fresh deterministic stage stream, or no tie randomisation for legacy calls."""
    return None if current_run_seed() is None else random.Random(seed_for(scope))

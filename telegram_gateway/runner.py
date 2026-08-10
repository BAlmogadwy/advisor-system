"""Getting the adviser off the webhook thread, without adding infrastructure.

Telegram redelivers any update whose webhook call does not return `200` promptly,
and an adviser turn is budgeted at up to four tool iterations against a 75-second
timeout each. Answering inside the request is therefore not slow-but-acceptable:
it is a loop, because the timeout produces a redelivery which produces a second
turn.

So the webhook acknowledges and this module does the work. It is the SAME shim
`core/services/planner_job_runner.py` already uses — a process-local
`ThreadPoolExecutor` plus `close_old_connections` on both sides — and it is
deliberately not more than that:

**Why not Celery or RQ.** Both need a broker. The project has no Redis and no
message broker of any kind; adding one for a single background call would make
the deployment depend on a service that nothing else needs, and Render would need
a second process type to run the worker. The existing durable option
(`core/services/timetable_repair_jobs.py`, drained by the `repair_worker`
management command) is the right escalation if delivery must survive a restart,
and is named here so the choice is a decision rather than an omission.

**What this shim does not give you.** It is process-local and not durable. If the
worker dies mid-turn the student's question stays `PENDING`, and the answer is
never delivered to the chat. That is survivable rather than silent: the question
and its state are in the same `AdvisorMessage` table the web thread reads, the row
is visible on the web adviser, and `advisor_turn.is_resumable` treats a turn
stranded past `STALE_GENERATION` as answerable again — so re-asking works instead
of being refused as a duplicate.
"""

from __future__ import annotations

import logging
import os
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

from django.conf import settings
from django.db import close_old_connections

logger = logging.getLogger(__name__)

_EXECUTOR: ThreadPoolExecutor | None = None

#: One worker. An adviser turn is bounded by the model, not by CPU, and a wider
#: pool would let one chat's burst occupy every slot — the per-student generation
#: budget is the real admission control and it is enforced inside the turn.
_MAX_WORKERS = 2


def _get_executor() -> ThreadPoolExecutor:
    global _EXECUTOR
    if _EXECUTOR is None:
        _EXECUTOR = ThreadPoolExecutor(
            max_workers=_MAX_WORKERS, thread_name_prefix="telegram-advisor"
        )
    return _EXECUTOR


def dispatch_sync() -> bool:
    """True when the turn must run inline.

    Under pytest the test database is a per-connection SQLite transaction that a
    background thread cannot see, so a dispatched turn would silently operate on
    an empty database. The same override exists for local debugging.
    """
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return True
    return bool(getattr(settings, "TELEGRAM_DISPATCH_SYNC", False))


def _worker(fn: Any, kwargs: dict[str, Any]) -> None:
    close_old_connections()
    try:
        fn(**kwargs)
    except Exception:  # noqa: BLE001
        # A background thread's exception is otherwise swallowed by the Future
        # nobody awaits. No payload in the log: the kwargs carry the question.
        logger.exception("telegram: background adviser turn failed")
    finally:
        close_old_connections()


def dispatch(fn: Any, **kwargs: Any) -> Future:
    """Run `fn(**kwargs)` off the request thread, or inline under pytest."""
    if dispatch_sync():
        future: Future = Future()
        try:
            fn(**kwargs)
            future.set_result(None)
        except BaseException as exc:  # noqa: BLE001
            logger.exception("telegram: inline adviser turn failed")
            future.set_exception(exc)
        return future
    return _get_executor().submit(_worker, fn, kwargs)


__all__ = ["dispatch", "dispatch_sync"]

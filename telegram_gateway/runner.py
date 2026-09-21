"""Test/local execution switch for the durable Telegram queue.

Production never launches an adviser turn from a web-process thread. The webhook
commits a ``TelegramUpdateReceipt``, returns 200, and the persistent
``telegram_advisor_worker`` leases that row. Under pytest (and only when an
operator explicitly opts in locally), the same durable job is drained inline so
transactional test databases remain visible and deterministic.
"""

from __future__ import annotations

import os

from django.conf import settings


def dispatch_sync() -> bool:
    """Whether a newly persisted job should be drained in the request process.

    Under pytest the test database is a per-connection SQLite transaction that a
    separate worker cannot see, so the test suite drains the real queue inline.
    The explicit setting exists only for local debugging; production keeps it
    false and runs the management-command worker.
    """

    if os.environ.get("PYTEST_CURRENT_TEST"):
        return True
    return bool(getattr(settings, "TELEGRAM_DISPATCH_SYNC", False))


__all__ = ["dispatch_sync"]

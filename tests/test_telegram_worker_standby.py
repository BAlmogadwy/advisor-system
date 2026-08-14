from __future__ import annotations

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from telegram_gateway.management.commands import telegram_advisor_worker as worker_command


@override_settings(TELEGRAM_ADVISOR_ENABLED=False)
def test_disabled_worker_remains_fail_closed_without_the_explicit_standby_flag() -> None:
    with pytest.raises(CommandError, match="TELEGRAM_ADVISOR_ENABLED"):
        call_command("telegram_advisor_worker", "--once")


@override_settings(TELEGRAM_ADVISOR_ENABLED=False)
def test_explicit_disabled_standby_never_reaches_the_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, float] = {}

    def standby(*, idle_sleep_seconds: float) -> None:
        seen["sleep"] = idle_sleep_seconds

    monkeypatch.setattr(worker_command, "run_disabled_standby", standby)
    monkeypatch.setattr(
        worker_command,
        "run_worker_loop",
        lambda **_kwargs: pytest.fail("disabled standby attempted to lease a queue row"),
    )

    call_command(
        "telegram_advisor_worker",
        "--standby-when-disabled",
        "--sleep",
        "2.5",
    )

    assert seen == {"sleep": 2.5}

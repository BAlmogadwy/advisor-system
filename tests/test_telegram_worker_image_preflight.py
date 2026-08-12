# mypy: ignore-errors
from __future__ import annotations

import subprocess
from contextlib import contextmanager

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from telegram_gateway.management.commands import telegram_advisor_worker as worker_command

# Preflight failure tests intentionally use small process/command doubles and
# platform-dependent monkeypatch callables. Production command code remains
# strictly typed; these adversarial doubles are deliberately structural.


class _FakeProcess:
    pid = 43210

    def __init__(self, wait_result=0, events: list[str] | None = None) -> None:
        self.wait_result = wait_result
        self.events = events

    def wait(self, *, timeout: float):
        if self.events is not None:
            self.events.append("browser-wait")
        assert 0 < timeout <= 60
        if isinstance(self.wait_result, BaseException):
            raise self.wait_result
        return self.wait_result

    def poll(self):
        return None

    def kill(self) -> None:
        if self.events is not None:
            self.events.append("browser-kill")


VALID_WORKER_SETTINGS = {
    "TELEGRAM_ADVISOR_ENABLED": True,
    "TELEGRAM_BOT_TOKEN": "123:abc",
    "TELEGRAM_PUBLIC_BASE_URL": "https://advisor.example.edu",
    "TELEGRAM_SEND_TIMETABLE_IMAGES": False,
    "LLM_BACKEND": "local",
    "LOCAL_LLM_BASE_URL": "http://127.0.0.1:1234/v1",
    "LOCAL_LLM_MODEL": "local-test-model",
}


@override_settings(**VALID_WORKER_SETTINGS)
def test_default_off_worker_never_runs_the_image_preflight(monkeypatch):
    monkeypatch.setattr(
        worker_command,
        "validate_worker_image_runtime",
        lambda: pytest.fail("the default-off worker launched an image preflight"),
    )
    monkeypatch.setattr(worker_command, "run_worker_loop", lambda **_kwargs: 0)

    call_command("telegram_advisor_worker", "--once")


@override_settings(**{**VALID_WORKER_SETTINGS, "TELEGRAM_SEND_TIMETABLE_IMAGES": True})
def test_image_enabled_worker_preflights_before_polling(monkeypatch):
    events: list[str] = []
    monkeypatch.setattr(
        worker_command,
        "validate_worker_image_runtime",
        lambda: events.append("preflight"),
    )
    monkeypatch.setattr(
        worker_command,
        "run_worker_loop",
        lambda **_kwargs: events.append("poll") or 0,
    )

    call_command("telegram_advisor_worker", "--once")

    assert events == ["preflight", "poll"]


@override_settings(**{**VALID_WORKER_SETTINGS, "TELEGRAM_SEND_TIMETABLE_IMAGES": True})
@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [
        (
            worker_command.ImageRuntimeValidationError("chromium_unavailable"),
            "chromium_unavailable",
        ),
        (RuntimeError("student 1234567 secret-path"), "image_runtime_unavailable"),
    ],
)
def test_image_preflight_failure_is_sanitized_and_blocks_polling(
    monkeypatch, failure, expected_code
):
    def fail_preflight():
        raise failure

    monkeypatch.setattr(worker_command, "validate_worker_image_runtime", fail_preflight)
    monkeypatch.setattr(
        worker_command,
        "run_worker_loop",
        lambda **_kwargs: pytest.fail("worker polled after a failed image preflight"),
    )

    with pytest.raises(CommandError) as caught:
        call_command("telegram_advisor_worker", "--once")

    message = str(caught.value)
    assert expected_code in message
    assert "student 1234567" not in message
    assert "secret-path" not in message


def _stub_assets(monkeypatch) -> None:
    monkeypatch.setattr(worker_command, "get_template", lambda _name: object())
    monkeypatch.setattr(worker_command, "missing_card_assets", lambda: [])


def _stub_origin(monkeypatch, events: list[str] | None = None) -> None:
    @contextmanager
    def origin():
        if events is not None:
            events.append("origin-enter")
        try:
            yield "http://127.0.0.1:43210"
        finally:
            if events is not None:
                events.append("origin-exit")

    monkeypatch.setattr(worker_command, "worker_card_origin", origin)
    monkeypatch.setattr(
        worker_command,
        "_probe_worker_card_origin",
        lambda origin_url: events.append("origin-probe") if events is not None else None,
    )


def test_image_runtime_preflight_resolves_assets_closes_origin_and_bounds_browser(
    monkeypatch,
):
    events: list[str] = []
    resolved: list[str] = []
    monkeypatch.setattr(
        worker_command, "get_template", lambda name: resolved.append(name) or object()
    )
    monkeypatch.setattr(
        worker_command,
        "missing_card_assets",
        lambda: resolved.append("card-assets") or [],
    )
    _stub_origin(monkeypatch, events)
    monkeypatch.setenv("PATH", "preflight-path")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "must-not-reach-browser")
    monkeypatch.setenv("ALIBABA_LLM_API_KEY", "must-not-reach-browser")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "must-not-reach-browser")

    def popen(command, **kwargs):
        events.append("browser-start")
        assert command[0] == worker_command.sys.executable
        assert command[1] == "-c"
        assert "sync_playwright" in command[2]
        assert ".chromium.launch" in command[2]
        assert "browser.close()" in command[2]
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["stdout"] == subprocess.DEVNULL
        assert kwargs["stderr"] == subprocess.DEVNULL
        assert kwargs["env"]["PATH"] == "preflight-path"
        assert "TELEGRAM_BOT_TOKEN" not in kwargs["env"]
        assert "ALIBABA_LLM_API_KEY" not in kwargs["env"]
        assert "TELEGRAM_WEBHOOK_SECRET" not in kwargs["env"]
        if worker_command.os.name == "nt":
            assert kwargs["creationflags"] == subprocess.CREATE_NEW_PROCESS_GROUP
            assert "start_new_session" not in kwargs
        else:
            assert kwargs["start_new_session"] is True
            assert "creationflags" not in kwargs
        return _FakeProcess(events=events)

    monkeypatch.setattr(worker_command.subprocess, "Popen", popen)

    worker_command.validate_worker_image_runtime()

    assert resolved == [worker_command._CARD_TEMPLATE, "card-assets"]
    assert events == [
        "origin-enter",
        "origin-probe",
        "origin-exit",
        "browser-start",
        "browser-wait",
    ]


def test_image_runtime_preflight_fails_before_browser_when_an_asset_is_missing(monkeypatch):
    monkeypatch.setattr(worker_command, "get_template", lambda _name: object())
    monkeypatch.setattr(
        worker_command,
        "missing_card_assets",
        lambda: ["img/side-decor1.png"],
    )
    monkeypatch.setattr(
        worker_command,
        "worker_card_origin",
        lambda: pytest.fail("origin started with a missing asset"),
    )
    monkeypatch.setattr(
        worker_command.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("browser launched with a missing asset"),
    )

    with pytest.raises(worker_command.ImageRuntimeValidationError) as caught:
        worker_command.validate_worker_image_runtime()

    assert caught.value.code == "card_assets_unavailable"


def test_image_runtime_preflight_sanitizes_an_origin_failure(monkeypatch):
    _stub_assets(monkeypatch)

    @contextmanager
    def broken_origin():
        raise RuntimeError("C:/secret/card/path")
        yield  # pragma: no cover - makes this a context manager.

    monkeypatch.setattr(worker_command, "worker_card_origin", broken_origin)
    monkeypatch.setattr(
        worker_command.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("browser launched after origin failure"),
    )

    with pytest.raises(worker_command.ImageRuntimeValidationError) as caught:
        worker_command.validate_worker_image_runtime()

    assert caught.value.code == "card_origin_unavailable"
    assert "secret" not in str(caught.value)


def test_image_runtime_preflight_probes_origin_and_sanitizes_probe_failure(monkeypatch):
    events: list[str] = []
    _stub_assets(monkeypatch)

    @contextmanager
    def origin():
        events.append("origin-enter")
        try:
            yield "http://127.0.0.1:43210"
        finally:
            events.append("origin-exit")

    monkeypatch.setattr(worker_command, "worker_card_origin", origin)
    monkeypatch.setattr(
        worker_command,
        "_probe_worker_card_origin",
        lambda _origin: (_ for _ in ()).throw(RuntimeError("C:/secret/probe")),
    )
    monkeypatch.setattr(
        worker_command.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("browser launched after probe failure"),
    )

    with pytest.raises(worker_command.ImageRuntimeValidationError) as caught:
        worker_command.validate_worker_image_runtime()

    assert caught.value.code == "card_origin_unavailable"
    assert "secret" not in str(caught.value)
    assert events == ["origin-enter", "origin-exit"]


def test_origin_probe_uses_private_header_no_proxy_and_bounded_timeout(monkeypatch):
    opened: list[str] = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, amount):
            assert amount == 1
            return b"/"

    class Opener:
        def open(self, request, *, timeout):
            opened.append(request.full_url)
            headers = {name.lower(): value for name, value in request.header_items()}
            assert headers["x-telegram-card-renderer"] == "renderer-proof"
            assert headers["x-forwarded-proto"] == "https"
            assert 0 < timeout <= 5
            return Response()

    def build_opener(*handlers):
        assert len(handlers) == 1
        assert isinstance(handlers[0], worker_command.ProxyHandler)
        return Opener()

    monkeypatch.setattr(worker_command, "sign_renderer_request", lambda: "renderer-proof")
    monkeypatch.setattr(worker_command, "build_opener", build_opener)

    worker_command._probe_worker_card_origin("http://127.0.0.1:43210/")

    assert opened == ["http://127.0.0.1:43210/telegram/card-assets/js/shared-timetable.js"]


@pytest.mark.parametrize(
    ("browser_result", "expected_code", "expects_termination"),
    [
        # A dead direct child can still leave Chromium/Node descendants in its
        # process group, so nonzero exit requires the same tree cleanup as a
        # timeout.
        (1, "chromium_unavailable", True),
        (subprocess.TimeoutExpired(["python"], 1), "chromium_timeout", True),
        (RuntimeError("secret wait failure"), "chromium_unavailable", True),
    ],
)
def test_image_runtime_preflight_sanitizes_browser_failure(
    monkeypatch, browser_result, expected_code, expects_termination
):
    _stub_assets(monkeypatch)
    _stub_origin(monkeypatch)
    process = _FakeProcess(wait_result=browser_result)
    terminated: list[object] = []

    monkeypatch.setattr(worker_command.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(
        worker_command,
        "_terminate_image_preflight_process_tree",
        lambda target: terminated.append(target),
    )

    with pytest.raises(worker_command.ImageRuntimeValidationError) as caught:
        worker_command.validate_worker_image_runtime()

    assert caught.value.code == expected_code
    assert terminated == ([process] if expects_termination else [])


def test_preflight_tree_cleanup_runs_after_direct_python_has_exited(monkeypatch):
    class DeadProcess(_FakeProcess):
        def poll(self):
            return 7

    process = DeadProcess(wait_result=7)
    killed: list[object] = []
    if worker_command.os.name == "nt":
        monkeypatch.setattr(
            worker_command.subprocess,
            "run",
            lambda command, **_kwargs: killed.append(command),
        )
    else:
        monkeypatch.setattr(
            worker_command.os,
            "killpg",
            lambda pid, sig: killed.append((pid, sig)),
        )

    worker_command._terminate_image_preflight_process_tree(process)

    if worker_command.os.name == "nt":
        assert killed and str(process.pid) in killed[0]
    else:
        assert killed == [(process.pid, worker_command.signal.SIGKILL)]

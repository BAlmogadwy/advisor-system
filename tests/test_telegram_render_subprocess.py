# mypy: ignore-errors
from __future__ import annotations

import io
import json
import logging
import os
from threading import Thread
from time import monotonic
from wsgiref.simple_server import make_server

import pytest

from telegram_gateway import render_child, rendering
from telegram_gateway.cards import sign_card, verify_renderer_request

# Adversarial process tests intentionally use duck-typed ``Popen`` doubles and
# platform-dependent monkeypatch callables. Production renderer modules remain
# strictly typed; forcing these test doubles into concrete subprocess types would
# hide the cleanup behaviours being exercised.


class _WritableBuffer(io.BytesIO):
    """Keep written input inspectable after production code closes stdin."""

    def close(self) -> None:
        self.was_closed = True


class _FakeProcess:
    def __init__(self, *, stdout: bytes, returncode: int | None = 0) -> None:
        self.stdin = _WritableBuffer()
        self.stdout = io.BytesIO(stdout)
        self.returncode = returncode
        self.pid = 43210
        self.killed = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None:
            raise TimeoutError
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9


def _signed_url(message_id: str = "123") -> str:
    return f"http://127.0.0.1:43210/telegram/card/{sign_card(message_id=message_id)}/"


def _install_fake_child(monkeypatch, process: _FakeProcess) -> dict:
    captured: dict = {}

    def popen(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return process

    monkeypatch.setattr(rendering.subprocess, "Popen", popen)
    return captured


def test_production_renderer_child_receives_no_application_secret(monkeypatch) -> None:
    secrets = {
        "DJANGO_SECRET_KEY": "django-secret-value",
        "DATABASE_URL": "postgres://db-secret",
        "TELEGRAM_BOT_TOKEN": "bot-secret-value",
        "TELEGRAM_WEBHOOK_SECRET": "webhook-secret-value",
        "ALIBABA_LLM_API_KEY": "llm-secret-value",
    }
    for name, value in secrets.items():
        monkeypatch.setenv(name, value)

    png = rendering.RecordingRenderer().png
    process = _FakeProcess(stdout=render_child.encode_response([png]))
    captured = _install_fake_child(monkeypatch, process)
    url = _signed_url()

    assert rendering.PlaywrightCardRenderer().render_many([url]) == [png]

    command = captured["command"]
    options = captured["kwargs"]
    child_env = options["env"]
    assert command[:2] == [rendering.sys.executable, "-I"]
    assert url not in " ".join(command)
    assert options["stdin"] is rendering.subprocess.PIPE
    assert options["stdout"] is rendering.subprocess.PIPE
    assert options["stderr"] is rendering.subprocess.DEVNULL
    assert options["close_fds"] is True
    assert set(child_env).issubset(set(rendering._RENDER_CHILD_ENV_NAMES))
    for name, value in secrets.items():
        assert name not in child_env
        assert value not in "\n".join(child_env.values())
        assert value not in " ".join(command)

    request = json.loads(process.stdin.getvalue())
    assert set(request) == {"version", "urls", "renderer_token"}
    assert request["urls"] == [url]
    assert verify_renderer_request(request["renderer_token"])


def test_production_renderer_child_failure_is_a_safe_empty_batch(monkeypatch, caplog) -> None:
    process = _FakeProcess(stdout=b"", returncode=7)
    _install_fake_child(monkeypatch, process)
    cleaned: list[int] = []
    monkeypatch.setattr(
        rendering,
        "_terminate_render_child_process_tree",
        lambda candidate: cleaned.append(candidate.pid),
    )
    url = _signed_url("child-failure")
    caplog.set_level(logging.WARNING)

    assert rendering.PlaywrightCardRenderer().render_many([url]) == []

    logged = " ".join(record.getMessage() for record in caplog.records)
    assert cleaned == [process.pid]
    assert "child failed" in logged
    assert url not in logged


def test_tree_cleanup_runs_after_the_direct_child_has_already_exited(monkeypatch) -> None:
    process = _FakeProcess(stdout=b"", returncode=7)
    killed: list[object] = []
    if rendering.os.name == "nt":
        monkeypatch.setattr(
            rendering.subprocess,
            "run",
            lambda command, **_kwargs: killed.append(command),
        )
    else:
        monkeypatch.setattr(
            rendering.os,
            "killpg",
            lambda pid, sig: killed.append((pid, sig)),
        )

    rendering._terminate_render_child_process_tree(process)

    if rendering.os.name == "nt":
        assert killed and str(process.pid) in killed[0]
    else:
        assert killed == [(process.pid, rendering.signal.SIGKILL)]


def test_production_renderer_hard_timeout_kills_the_process_tree(monkeypatch, caplog) -> None:
    process = _FakeProcess(stdout=b"", returncode=None)
    _install_fake_child(monkeypatch, process)
    killed: list[int] = []

    def terminate(candidate):
        killed.append(candidate.pid)
        candidate.kill()

    monkeypatch.setattr(rendering, "_terminate_render_child_process_tree", terminate)
    monkeypatch.setattr(rendering, "RENDER_BATCH_TIMEOUT_SECONDS", 0.03)
    url = _signed_url("timeout")
    caplog.set_level(logging.WARNING)

    started = monotonic()
    assert rendering.PlaywrightCardRenderer().render_many([url]) == []
    elapsed = monotonic() - started

    assert elapsed < 0.5
    assert killed == [process.pid]
    assert process.killed
    logged = " ".join(record.getMessage() for record in caplog.records)
    assert "hard deadline" in logged
    assert url not in logged


def test_production_renderer_kills_a_child_that_exceeds_the_output_bound(
    monkeypatch, caplog
) -> None:
    limit = rendering._render_child_output_limit(1)
    process = _FakeProcess(stdout=b"x" * (limit + 1), returncode=0)
    _install_fake_child(monkeypatch, process)
    killed: list[int] = []

    def terminate(candidate):
        killed.append(candidate.pid)
        candidate.kill()

    monkeypatch.setattr(rendering, "_terminate_render_child_process_tree", terminate)
    caplog.set_level(logging.WARNING)

    assert rendering.PlaywrightCardRenderer().render_many([_signed_url("oversize")]) == []

    assert killed == [process.pid]
    assert any("output limit" in record.getMessage() for record in caplog.records)


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:43210/telegram/card/not-signed/",
        "http://[::1]:43210/telegram/card/not-signed/",
        "http://127.0.0.1:43210/telegram/card/not-signed/",
        "http://127.0.0.1:43210/student/",
        "http://advisor.example.edu/telegram/card/not-signed/",
    ],
)
def test_production_renderer_refuses_nonlocal_or_unsigned_urls(monkeypatch, url: str) -> None:
    monkeypatch.setattr(
        rendering.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("invalid URL reached the child process"),
    )
    assert rendering.PlaywrightCardRenderer().render_many([url]) == []


@pytest.mark.skipif(
    os.environ.get("RUN_TELEGRAM_BROWSER_TEST") != "1",
    reason="set RUN_TELEGRAM_BROWSER_TEST=1 for the real Chromium smoke test",
)
def test_real_isolated_child_renders_a_png() -> None:
    body = (
        "<!doctype html><html><body>"
        '<div id="sa-card-root" data-card-ready="1" '
        'style="width:320px;height:120px;background:#e8fff8;color:#073b32">'
        "جدول الطالب — Timetable</div></body></html>"
    ).encode()

    def application(_environ, start_response):
        start_response(
            "200 OK",
            [
                ("Content-Type", "text/html; charset=utf-8"),
                ("Content-Length", str(len(body))),
            ],
        )
        return [body]

    server = make_server("127.0.0.1", 0, application)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        token = sign_card(message_id="real-child-render")
        url = f"http://127.0.0.1:{server.server_port}/telegram/card/{token}/"
        images = rendering.PlaywrightCardRenderer().render_many([url])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert len(images) == 1
    assert images[0] is not None
    assert images[0].startswith(b"\x89PNG\r\n\x1a\n")

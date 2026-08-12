# mypy: ignore-errors
from __future__ import annotations

import logging
from threading import Event, Thread
from time import monotonic, sleep
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import ProxyHandler, build_opener

import pytest
from django.test import override_settings

from telegram_gateway import rendering

# Origin lifecycle tests intentionally replace WSGI/thread primitives with
# minimal duck-typed doubles. Production origin code remains strictly typed;
# concrete framework annotations would make these failure-path tests less clear.


def _get(url: str):
    # Do not let a developer's ambient proxy turn a loopback-only test into an
    # external request.
    return build_opener(ProxyHandler({})).open(url, timeout=2)  # noqa: S310


@pytest.mark.parametrize(
    ("configured", "port", "expected"),
    [
        ("http://localhost:8123/", None, "http://localhost:8123"),
        ("", "8002", "http://127.0.0.1:8002"),
    ],
)
def test_worker_card_origin_reuses_an_explicit_local_origin(
    monkeypatch, configured: str, port: str | None, expected: str
) -> None:
    def must_not_start_server(*_args, **_kwargs):
        raise AssertionError("an explicit local render origin started another server")

    monkeypatch.setattr(rendering, "make_server", must_not_start_server)
    with override_settings(TELEGRAM_INTERNAL_BASE_URL=configured):
        with rendering.worker_card_origin(port) as origin:
            assert origin == expected


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ("http://127.0.0.1:9000/", "http://127.0.0.1:9000"),
        ("http://localhost:9000", "http://localhost:9000"),
    ],
)
def test_local_base_url_accepts_only_plain_http_loopback_origins(
    configured: str, expected: str
) -> None:
    with override_settings(TELEGRAM_INTERNAL_BASE_URL=configured):
        assert rendering.local_base_url() == expected


@pytest.mark.parametrize(
    "configured",
    [
        "https://localhost:9000",
        "ftp://localhost:9000",
        "file://localhost/tmp/card",
        "http://user:secret@localhost:9000",
        "http://localhost:9000/a/path",
        "http://localhost:9000?student=1",
        "http://localhost:9000#fragment",
        "http://localhost:not-a-port",
        "http://0.0.0.0:9000",
        "http://[::1]:9000",
        "//localhost:9000",
    ],
)
def test_local_base_url_rejects_non_origin_or_non_loopback_values(configured: str) -> None:
    with override_settings(TELEGRAM_INTERNAL_BASE_URL=configured):
        assert rendering.local_base_url() == ""


@override_settings(TELEGRAM_INTERNAL_BASE_URL="")
def test_worker_card_origin_is_ephemeral_card_only_and_silent(monkeypatch, caplog, capsys) -> None:
    signed_token = "signed-bearer-value-that-must-not-be-logged"
    requested_paths: list[str] = []
    application_count = 0

    def application(environ, start_response):
        requested_paths.append(str(environ.get("PATH_INFO") or ""))
        # Django's request logger includes request.path for 500 responses. Emit
        # an exception with the same sensitive path in both its message and
        # traceback: diagnostics must survive while the bearer token does not.
        path = str(environ.get("PATH_INFO") or "")
        try:
            raise RuntimeError(f"Synthetic request failure at {path}")
        except RuntimeError:
            logging.getLogger("django.request").exception("Synthetic request record: %s", path)
        start_response(
            "200 OK",
            [("Content-Type", "text/plain"), ("Content-Length", "2")],
        )
        return [b"ok"]

    def get_application():
        nonlocal application_count
        application_count += 1
        return application

    real_make_server = rendering.make_server
    servers = []

    def recording_make_server(*args, **kwargs):
        server = real_make_server(*args, **kwargs)
        servers.append(server)
        return server

    monkeypatch.setattr(rendering, "get_wsgi_application", get_application)
    monkeypatch.setattr(rendering, "make_server", recording_make_server)
    caplog.set_level(logging.WARNING)
    request_logger = logging.getLogger("django.request")
    monkeypatch.setattr(request_logger, "handlers", [*request_logger.handlers, caplog.handler])

    with rendering.worker_card_origin() as origin:
        parsed = urlparse(origin)
        assert parsed.scheme == "http"
        assert parsed.hostname == "127.0.0.1"
        assert isinstance(parsed.port, int) and parsed.port > 0

        with _get(f"{origin}/telegram/card/{signed_token}/") as response:
            assert response.status == 200
            assert response.read() == b"ok"
        with _get(f"{origin}/telegram/card-assets/card.js") as response:
            assert response.status == 200

        # The worker-local listener is not a second copy of the student portal.
        for blocked_path in ("/student/", "/static/js/shared-timetable.js"):
            with pytest.raises(HTTPError) as blocked:
                _get(f"{origin}{blocked_path}")
            assert blocked.value.code == 404

        assert application_count == 1
        assert len(servers) == 1, "one delivery batch started more than one origin"
        assert servers[0].server_address[0] == "127.0.0.1"
        assert requested_paths == [
            f"/telegram/card/{signed_token}/",
            "/telegram/card-assets/card.js",
        ]

    # Redaction is a permanent logging safety invariant, not a filter temporarily
    # removed when one origin context closes.
    logging.getLogger("django.request").error("After context: /telegram/card/%s/", signed_token)
    assert servers[0].socket.fileno() == -1, "the loopback listener survived its context"
    captured = capsys.readouterr()
    assert signed_token not in caplog.text
    assert signed_token not in captured.out
    assert signed_token not in captured.err
    diagnostics = f"{caplog.text}\n{captured.err}"
    assert "Synthetic request record" in diagnostics
    assert "Synthetic request failure" in diagnostics
    assert "RuntimeError" in diagnostics
    assert "After context" in diagnostics
    assert "/telegram/card/<redacted>/" in diagnostics


@override_settings(TELEGRAM_INTERNAL_BASE_URL="")
def test_worker_card_origin_always_closes_after_a_caller_exception(monkeypatch) -> None:
    def application(_environ, start_response):
        start_response("204 No Content", [("Content-Length", "0")])
        return [b""]

    real_make_server = rendering.make_server
    servers = []

    def recording_make_server(*args, **kwargs):
        server = real_make_server(*args, **kwargs)
        servers.append(server)
        return server

    monkeypatch.setattr(rendering, "get_wsgi_application", lambda: application)
    monkeypatch.setattr(rendering, "make_server", recording_make_server)

    with pytest.raises(RuntimeError, match="render failed"):
        with rendering.worker_card_origin():
            raise RuntimeError("render failed")

    assert len(servers) == 1
    assert servers[0].socket.fileno() == -1, "exceptional exit left the listener open"


@override_settings(TELEGRAM_INTERNAL_BASE_URL="")
def test_worker_card_origin_refuses_a_second_concurrent_listener(monkeypatch) -> None:
    def application(_environ, start_response):
        start_response("204 No Content", [("Content-Length", "0")])
        return [b""]

    monkeypatch.setattr(rendering, "get_wsgi_application", lambda: application)

    with rendering.worker_card_origin():
        with pytest.raises(RuntimeError, match="another worker card origin is active"):
            with rendering.worker_card_origin():
                pass

    # The guard is released after an ordinary batch completes.
    with rendering.worker_card_origin():
        pass


@override_settings(TELEGRAM_INTERNAL_BASE_URL="")
def test_worker_card_origin_bounds_and_quarantines_a_stuck_request(
    monkeypatch,
) -> None:
    entered = Event()
    release = Event()
    client: Thread | None = None

    def application(_environ, start_response):
        entered.set()
        release.wait(timeout=5)
        start_response("204 No Content", [("Content-Length", "0")])
        return [b""]

    real_make_server = rendering.make_server
    servers = []

    def recording_make_server(*args, **kwargs):
        server = real_make_server(*args, **kwargs)
        servers.append(server)
        return server

    monkeypatch.setattr(rendering, "get_wsgi_application", lambda: application)
    monkeypatch.setattr(rendering, "make_server", recording_make_server)
    monkeypatch.setattr(rendering, "ORIGIN_REQUEST_JOIN_TIMEOUT_SECONDS", 0.05)

    def make_request(url: str) -> None:
        try:
            with _get(url) as response:
                response.read()
        except Exception:  # noqa: BLE001 - socket closure is acceptable in this test.
            pass

    cleanup_started = monotonic()
    try:
        with rendering.worker_card_origin() as origin:
            client = Thread(target=make_request, args=(f"{origin}/telegram/card/token/",))
            client.start()
            assert entered.wait(timeout=1), "the request never reached the WSGI app"

        assert monotonic() - cleanup_started < 1.0, "origin cleanup waited indefinitely"
        assert servers[0].live_request_threads(), "the fixture did not leave a stuck request"
        with pytest.raises(RuntimeError, match="previous worker card origin is still stopping"):
            with rendering.worker_card_origin():
                pass
        assert len(servers) == 1, "a lingering request was allowed to create another server"
    finally:
        release.set()
        if client is not None:
            client.join(timeout=2)

    deadline = monotonic() + 2
    while servers[0].live_request_threads() and monotonic() < deadline:
        sleep(0.01)
    assert not servers[0].live_request_threads()

    # Once the quarantined handler has actually finished, the next batch may run.
    with rendering.worker_card_origin():
        pass
    assert len(servers) == 2

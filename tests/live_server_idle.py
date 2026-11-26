"""Keep a browser test's database work apart from the live server's.

A page asks the live server for more than its test waits for: a list goes on
loading after the title the test checks was set, a request the page abandons
for the next one still runs to the end on the server (an aborted fetch stops
the browser waiting, not the server working), and some pages ask again on a
timer. Such a request can still be using the database when the test ends and
Django flushes it, or while the test writes or reads rows of its own.

On the in-memory SQLite test database that meeting fails at once. The
server's threads share one connection. The test thread has another: Playwright's
sync API leaves its event loop marked as running in that thread, and Django
gives a thread in that state a connection of its own. The two connections meet
in SQLite's shared cache, where a table one of them is writing (or, for a
writer, a table the other is reading) locks the other out, and the busy timeout
does not wait. The test thread gets "database table is locked", and a flush
gets "couldn't be flushed" (twice on CI in November 2026, after the test itself
had passed).

`IdleTrackingLiveServerThread` serves through a server that counts the
connections it has taken and not finished with, and the requests inside the
application. Two ways to use it:

* At the end of a test, its pages closed: `wait_for_live_server()` opens one
  more connection and waits for the server to close it. The server takes
  connections one at a time, in the order they arrived, so by then it has taken
  and counted every connection a page opened before. Then it waits until no
  connection is open. `install()` (called by tests/conftest.py) makes every
  LiveServerTestCase serve this way and wait like this before its flush, so a
  test only has to close its pages in its cleanups or its tearDown.
* During a test, its pages still open: `with live_server_paused(...)` holds
  every new request at the application's door, waits until none is inside, and
  lets the held ones in when the block ends. Do the test's own database work
  inside it, and nothing that waits for the server: a page action that loads
  something would wait for a request the block is holding.
"""

from __future__ import annotations

import contextlib
import functools
import socket
import threading
from collections.abc import Callable, Iterator
from typing import Any

from django.core.servers.basehttp import ThreadedWSGIServer
from django.test import LiveServerTestCase
from django.test.testcases import LiveServerThread


class IdleTrackingWSGIServer(ThreadedWSGIServer):
    """The live server, counting open connections and the requests inside the application."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._open = 0
        self._inside = 0
        self._paused = False
        self._changed = threading.Condition()
        self._thread = threading.local()
        super().__init__(*args, **kwargs)

    # ── Connections: taken, then finished ──

    def process_request(self, request: Any, client_address: Any) -> None:
        # On the serving thread, as it takes each connection, in arrival order.
        with self._changed:
            self._open += 1
        try:
            super().process_request(request, client_address)  # starts its thread
        except BaseException:
            self._finished()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        # The connection's own thread: every request on it, then the close.
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._leave()  # an answer the server never closed (a broken connection)
            self._finished()

    def _finished(self) -> None:
        with self._changed:
            self._open -= 1
            self._changed.notify_all()

    def wait_until_idle(self, timeout: float) -> bool:
        """True once no connection is open; False if `timeout` seconds pass first."""
        with self._changed:
            return self._changed.wait_for(lambda: self._open == 0, timeout)

    # ── Requests: through the application's door, then answered ──

    def set_app(self, application: Any) -> None:
        super().set_app(self._counted(application))

    def _counted(self, application: Callable[..., Any]) -> Callable[..., Any]:
        def counted(environ: dict[str, Any], start_response: Callable[..., Any]) -> Any:
            self._enter()
            try:
                answer = application(environ, start_response)
            except BaseException:
                self._leave()
                raise
            return _Answer(answer, self._leave)

        return counted

    def _enter(self) -> None:
        with self._changed:
            self._changed.wait_for(lambda: not self._paused)
            self._inside += 1
        self._thread.inside = True

    def _leave(self) -> None:
        # On the connection's thread, which handles one request at a time.
        if not getattr(self._thread, "inside", False):
            return
        self._thread.inside = False
        with self._changed:
            self._inside -= 1
            self._changed.notify_all()

    @contextlib.contextmanager
    def paused(self, timeout: float) -> Iterator[None]:
        """Hold new requests at the door until the block ends; enter once none is inside."""
        with self._changed:
            if self._paused:
                raise RuntimeError("The live server is already paused.")
            self._paused = True
            if not self._changed.wait_for(lambda: self._inside == 0, timeout):
                self._paused = False
                self._changed.notify_all()
                raise AssertionError(
                    f"The live server was still answering a request after {timeout:g} s."
                )
        try:
            yield
        finally:
            with self._changed:
                self._paused = False
                self._changed.notify_all()


class _Answer:
    """An application's answer, counted out once the server has closed it."""

    def __init__(self, answer: Any, done: Callable[[], None]) -> None:
        self._answer = answer
        self._done = done

    def __iter__(self) -> Iterator[bytes]:
        return iter(self._answer)

    def __len__(self) -> int:
        # wsgiref asks, to set a Content-Length; a TypeError means "no length", as before.
        return len(self._answer)

    def close(self) -> None:
        try:
            close = getattr(self._answer, "close", None)
            if close is not None:
                close()
        finally:
            self._done()


class IdleTrackingLiveServerThread(LiveServerThread):
    """`server_thread_class` for a LiveServerTestCase; `install()` makes it the default."""

    server_class = IdleTrackingWSGIServer


def _tracking_server(server_thread: LiveServerThread) -> IdleTrackingWSGIServer:
    server = server_thread.httpd
    if not isinstance(server, IdleTrackingWSGIServer):
        raise TypeError(
            "Serve the test class with server_thread_class = IdleTrackingLiveServerThread."
        )
    return server


def wait_for_live_server(server_thread: LiveServerThread, timeout: float = 60) -> None:
    """Return once the live server has finished every connection opened so far.

    Close the pages first: an open page keeps its connections open, and can
    start a new request at any moment.
    """
    server = _tracking_server(server_thread)
    try:
        # Last in line: a connection with no request, which the server closes
        # as soon as it takes it.
        address = server.socket.getsockname()[:2]
        with socket.create_connection(address, timeout=timeout) as probe:
            probe.shutdown(socket.SHUT_WR)
            while probe.recv(1024):
                pass
    except TimeoutError as exc:
        raise AssertionError(
            f"The live server took no new connection within {timeout:g} s."
        ) from exc
    if not server.wait_until_idle(timeout):
        raise AssertionError(
            f"The live server still had a connection open {timeout:g} s after the pages "
            "closed. Close every page a test opens in its cleanups or its tearDown."
        )


def live_server_paused(
    server_thread: LiveServerThread, timeout: float = 60
) -> contextlib.AbstractContextManager[None]:
    """Hold the live server's new requests while the block does the test's database work."""
    return _tracking_server(server_thread).paused(timeout)


def install() -> None:
    """Make every LiveServerTestCase track its server, and wait for it before the flush."""
    if LiveServerTestCase.server_thread_class is LiveServerThread:
        LiveServerTestCase.server_thread_class = IdleTrackingLiveServerThread
    flush = LiveServerTestCase._fixture_teardown  # type: ignore[attr-defined]
    if getattr(flush, "waits_for_live_server", False):
        return

    @functools.wraps(flush)
    def _fixture_teardown(self: LiveServerTestCase) -> None:
        server_thread = getattr(type(self), "server_thread", None)
        try:
            if server_thread is not None and isinstance(
                getattr(server_thread, "httpd", None), IdleTrackingWSGIServer
            ):
                wait_for_live_server(server_thread)
        finally:
            flush(self)

    _fixture_teardown.waits_for_live_server = True  # type: ignore[attr-defined]
    LiveServerTestCase._fixture_teardown = _fixture_teardown  # type: ignore[attr-defined]

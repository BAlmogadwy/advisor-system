"""Let a browser test wait until the live server has answered its pages.

A page asks the live server for more than its test waits for: a list goes on
loading after the title the test checks was set, and a request the page
abandons for the next one still runs to the end on the server (an aborted
fetch stops the browser waiting, not the server working). Such a request can
still be writing to the database when the test ends and Django flushes it, or
when the test writes the user and session of its next page.

On the in-memory SQLite test database that meeting fails at once. The
server's threads share one connection. The test thread has another: Playwright's
sync API leaves its event loop marked as running in that thread, and Django
gives a thread in that state a connection of its own. The two connections meet
in SQLite's shared cache, where an open write transaction locks the other
connection out and the busy timeout does not wait. The test thread gets
"database table is locked", and a flush gets "couldn't be flushed" (twice on
CI in November 2026, after the test itself had passed).

`IdleTrackingLiveServerThread` serves through a server that counts the
connections it has taken and not yet finished with. `wait_for_live_server()`
opens one more connection and waits for the server to close it. The server
takes connections one at a time, in the order they arrived, so by then it has
taken and counted every connection a page opened before. Then it waits until
the count is zero. Close the pages first, or one of them can start a new request
at any moment.
"""

from __future__ import annotations

import socket
import threading
from typing import Any

from django.core.servers.basehttp import ThreadedWSGIServer
from django.test.testcases import LiveServerThread


class IdleTrackingWSGIServer(ThreadedWSGIServer):
    """The live server, counting the connections it has taken and not finished with."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._open = 0
        self._changed = threading.Condition()
        super().__init__(*args, **kwargs)

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
            self._finished()

    def _finished(self) -> None:
        with self._changed:
            self._open -= 1
            self._changed.notify_all()

    def wait_until_idle(self, timeout: float) -> bool:
        """True once no connection is open; False if `timeout` seconds pass first."""
        with self._changed:
            return self._changed.wait_for(lambda: self._open == 0, timeout)


class IdleTrackingLiveServerThread(LiveServerThread):
    """`server_thread_class` for a LiveServerTestCase that waits with `wait_for_live_server`."""

    server_class = IdleTrackingWSGIServer


def wait_for_live_server(server_thread: LiveServerThread, timeout: float = 60) -> None:
    """Return once the live server has finished every connection opened so far."""
    server = server_thread.httpd
    if not isinstance(server, IdleTrackingWSGIServer):
        raise TypeError(
            "Serve the test class with server_thread_class = IdleTrackingLiveServerThread."
        )
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
            f"The live server was still answering a request {timeout:g} s after the pages closed."
        )

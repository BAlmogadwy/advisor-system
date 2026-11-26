"""`wait_for_live_server` waits for everything the live server has not finished.

Proved with plain sockets, no browser and no view: a connection still open
keeps it waiting, and so does one the server has taken but not yet counted,
which only the probe connection, queued behind it, can see
(tests/live_server_idle.py). The wait runs on a thread of its own with a
generous timeout, so a slow machine can only make a check here pass late,
never fail: "not finished yet" is asked after half a second, "finished" is
given ten.
"""

from __future__ import annotations

import socket
import threading
from typing import Any

from django.test import LiveServerTestCase

from tests.live_server_idle import (
    IdleTrackingLiveServerThread,
    IdleTrackingWSGIServer,
    wait_for_live_server,
)


class WaitForLiveServerTests(LiveServerTestCase):
    server_thread_class = IdleTrackingLiveServerThread
    # No request here reaches a view, so no test database either.
    databases: set[str] = set()

    def _connect(self) -> socket.socket:
        """A connection that sends nothing: its thread waits to read a request."""
        connection = socket.create_connection(
            self.server_thread.httpd.socket.getsockname()[:2], timeout=10
        )
        self.addCleanup(connection.close)
        return connection

    def _waiting(self) -> threading.Event:
        """Start `wait_for_live_server` on a thread; the event is set once it returns."""
        finished = threading.Event()
        errors: list[BaseException] = []

        def wait() -> None:
            try:
                wait_for_live_server(self.server_thread, timeout=10)
            except BaseException as error:
                errors.append(error)
            finally:
                finished.set()

        threading.Thread(target=wait, daemon=True).start()
        self.addCleanup(lambda: self.assertEqual(errors, []))
        return finished

    def test_it_waits_while_a_connection_is_open(self) -> None:
        connection = self._connect()
        finished = self._waiting()
        self.assertFalse(finished.wait(0.5))
        connection.close()
        self.assertTrue(finished.wait(10))

    def test_it_waits_for_a_connection_taken_but_not_counted(self) -> None:
        server = self.server_thread.httpd
        assert isinstance(server, IdleTrackingWSGIServer)
        taken = threading.Event()
        release = threading.Event()
        verify = server.verify_request

        def held(request: Any, client_address: Any) -> bool:
            # On the serving thread, between taking a connection and counting it.
            taken.set()
            release.wait(10)
            return bool(verify(request, client_address))

        server.verify_request = held  # type: ignore[method-assign]
        self.addCleanup(vars(server).pop, "verify_request", None)
        self.addCleanup(release.set)
        connection = self._connect()
        self.assertTrue(taken.wait(10))
        # By the count alone the server would be idle.
        self.assertTrue(server.wait_until_idle(0))
        # The probe queues behind the held connection, so the wait goes on.
        finished = self._waiting()
        self.assertFalse(finished.wait(0.5))
        release.set()
        # Taken and counted now; its thread waits for a request.
        self.assertFalse(finished.wait(0.5))
        connection.close()
        self.assertTrue(finished.wait(10))

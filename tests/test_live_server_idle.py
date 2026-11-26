"""The live server waits and pauses as tests/live_server_idle.py says.

Proved with plain sockets and two views of this module's own, which answer
only when the test lets them; no browser and no middleware. A slow machine can
only make a check here pass late, never fail: "not yet" is asked after half a
second, "done" is given ten.
"""

from __future__ import annotations

import http.client
import socket
import threading
from typing import Any
from unittest import mock

from django.contrib.auth import get_user_model
from django.http import HttpRequest, HttpResponse, StreamingHttpResponse
from django.test import LiveServerTestCase, override_settings
from django.urls import path

from tests.live_server_idle import (
    IdleTrackingLiveServerThread,
    IdleTrackingWSGIServer,
    live_server_paused,
    wait_for_live_server,
)

# The views: each says it was reached, then answers once the test releases it.
REACHED = threading.Event()
RELEASE = threading.Event()


def _held(request: HttpRequest) -> HttpResponse:
    REACHED.set()
    RELEASE.wait(10)
    response = HttpResponse(b"answered")
    # With its length known the server keeps the connection for the next request.
    response["Content-Length"] = str(len(response.content))
    return response


def _streamed(request: HttpRequest) -> StreamingHttpResponse:
    # The view has returned before its body is made: the server makes it while
    # it sends it, as a streamed list read from the database would be.
    def body() -> Any:
        yield b"answered "
        REACHED.set()
        RELEASE.wait(10)
        yield b"in two parts"

    return StreamingHttpResponse(body())


urlpatterns = [path("held/", _held), path("streamed/", _streamed)]


@override_settings(ROOT_URLCONF=__name__, MIDDLEWARE=[])
class LiveServerIdleTests(LiveServerTestCase):
    # No view here queries the database, but the class keeps its access: in a
    # full run the server thread inherits an open connection, and every request
    # checks it (close_old_connections), which a database-less class refuses.

    def setUp(self) -> None:
        REACHED.clear()
        RELEASE.clear()
        self.addCleanup(RELEASE.set)

    def _connect(self) -> socket.socket:
        """A connection that sends nothing: its thread waits to read a request."""
        connection = socket.create_connection(
            self.server_thread.httpd.socket.getsockname()[:2], timeout=10
        )
        self.addCleanup(connection.close)
        return connection

    def _ask_in_background(
        self, url: str = "/held/", answer: bytes = b"answered"
    ) -> threading.Event:
        """GET a view on a thread; the event is set once its whole answer has arrived."""
        answered = threading.Event()
        host, port = self.server_thread.httpd.socket.getsockname()[:2]

        def ask() -> None:
            connection = http.client.HTTPConnection(host, port, timeout=10)
            try:
                connection.request("GET", url)
                if connection.getresponse().read() == answer:
                    answered.set()
            finally:
                connection.close()

        threading.Thread(target=ask, daemon=True).start()
        return answered

    def _in_background(self, wait: Any) -> threading.Event:
        """Run a wait on a thread; the event is set once it has returned without error."""
        finished = threading.Event()
        errors: list[BaseException] = []

        def run() -> None:
            try:
                wait()
            except BaseException as error:
                errors.append(error)
            else:
                finished.set()

        threading.Thread(target=run, daemon=True).start()
        self.addCleanup(lambda: self.assertEqual(errors, []))
        return finished

    # ── At the end of a test: every connection finished ──

    def test_the_wait_goes_on_while_a_connection_is_open(self) -> None:
        connection = self._connect()
        finished = self._in_background(lambda: wait_for_live_server(self.server_thread, 10))
        self.assertFalse(finished.wait(0.5))
        connection.close()
        self.assertTrue(finished.wait(10))

    def test_the_wait_goes_on_for_a_connection_taken_but_not_counted(self) -> None:
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
        finished = self._in_background(lambda: wait_for_live_server(self.server_thread, 10))
        self.assertFalse(finished.wait(0.5))
        release.set()
        # Taken and counted now; its thread waits for a request.
        self.assertFalse(finished.wait(0.5))
        connection.close()
        self.assertTrue(finished.wait(10))

    def test_every_live_server_test_waits_for_its_server_before_the_flush(self) -> None:
        self.assertIs(LiveServerTestCase.server_thread_class, IdleTrackingLiveServerThread)
        with mock.patch("tests.live_server_idle.wait_for_live_server") as wait:
            self._fixture_teardown()  # type: ignore[attr-defined]
        wait.assert_called_once_with(self.server_thread)

    def test_the_flush_still_runs_when_the_wait_fails(self) -> None:
        # Left unflushed, a timed-out test would hand its rows to the next one.
        get_user_model().objects.create_user(username="left-behind")
        failing = mock.patch(
            "tests.live_server_idle.wait_for_live_server",
            side_effect=AssertionError("still answering"),
        )
        with failing, self.assertRaisesRegex(AssertionError, "still answering"):
            self._fixture_teardown()  # type: ignore[attr-defined]
        self.assertFalse(get_user_model().objects.filter(username="left-behind").exists())

    # ── During a test: new requests held at the door ──

    def test_a_pause_waits_for_a_request_inside_the_application(self) -> None:
        answered = self._ask_in_background()
        self.assertTrue(REACHED.wait(10))

        def pause() -> None:
            with live_server_paused(self.server_thread, 10):
                pass

        paused = self._in_background(pause)
        self.assertFalse(paused.wait(0.5))
        RELEASE.set()
        self.assertTrue(answered.wait(10))
        self.assertTrue(paused.wait(10))

    def test_a_streamed_answer_is_inside_until_the_server_has_closed_it(self) -> None:
        answered = self._ask_in_background("/streamed/", b"answered in two parts")
        # The view returned long ago; its body is half sent.
        self.assertTrue(REACHED.wait(10))

        def pause() -> None:
            with live_server_paused(self.server_thread, 10):
                pass

        paused = self._in_background(pause)
        self.assertFalse(paused.wait(0.5))
        RELEASE.set()
        self.assertTrue(answered.wait(10))
        self.assertTrue(paused.wait(10))

    def test_a_pause_holds_a_new_request_at_the_door_until_it_ends(self) -> None:
        RELEASE.set()
        with live_server_paused(self.server_thread, 10):
            answered = self._ask_in_background()
            self.assertFalse(REACHED.wait(0.5))
        self.assertTrue(REACHED.wait(10))
        self.assertTrue(answered.wait(10))

    def test_a_kept_alive_connection_waiting_for_its_next_request_does_not_hold_a_pause(
        self,
    ) -> None:
        # A browser keeps its connections between requests: once its answer is
        # closed, a request is out, though its connection stays open.
        RELEASE.set()
        host, port = self.server_thread.httpd.socket.getsockname()[:2]
        connection = http.client.HTTPConnection(host, port, timeout=10)
        self.addCleanup(connection.close)
        connection.request("GET", "/held/")
        response = connection.getresponse()
        self.assertEqual(response.read(), b"answered")
        self.assertFalse(response.will_close)
        with live_server_paused(self.server_thread, 10):
            pass

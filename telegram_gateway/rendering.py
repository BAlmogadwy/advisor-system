"""Turning the card page into a PNG, and failing quietly when it cannot.

Two rules govern this module, and both are about what happens when it does not
work.

**A failed screenshot must never cost the student their answer.** The answer is
already generated, validated and stored by the time anything here runs. So every
path returns `None` rather than raising: no Chromium installed, no browser
launch, a render timeout, a broken page — all of them degrade to "send the text
and the link", which is exactly the behaviour that shipped before images existed.

**It must be replaceable in tests.** Same shape as the transport: a module-level
renderer swapped by `set_renderer`, plus an autouse fixture in `tests/conftest.py`
that refuses to start a browser at all. Convention was not enough: the LLM client
got a network guard only after a test reached the internet for real, and this is
the same hazard one module over.

A correction worth keeping written down: an earlier version of this docstring said
Chromium was **not** installed on Render. It is — `build.sh` has run
`playwright install chromium` since 2026-04-08. That mistake was not harmless. It
told an operator to expect exactly the symptom the real bugs produced, so
`card render failed` would have been explained away rather than investigated.
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import struct
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from socketserver import ThreadingMixIn
from threading import Event, Lock, Thread, current_thread
from time import monotonic
from typing import Any, Protocol
from urllib.parse import urlparse
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

from django.conf import settings
from django.core.wsgi import get_wsgi_application

from . import render_child as _render_child_protocol

logger = logging.getLogger(__name__)

#: Wall-clock ceiling for one card. A browser that has not produced a picture by
#: now is not going to; the turn should not wait on it.
RENDER_TIMEOUT_MS = _render_child_protocol.RENDER_TIMEOUT_MS

#: Browser viewport for the 720px `#sa-card-root` in card.html, with a small
#: safety margin. The element itself is tightly cropped by the child renderer.
VIEWPORT = _render_child_protocol.VIEWPORT

#: The in-browser operations have their own per-step deadlines, but browser.close
#: does not. The whole Python/Playwright/Chromium process tree gets this one hard
#: outer limit, after which it is killed and delivery safely falls back to text.
RENDER_BATCH_TIMEOUT_SECONDS = 60.0
RENDER_CHILD_KILL_TIMEOUT_SECONDS = 5.0

_RENDER_CHILD_ENV_NAMES = (
    "COMSPEC",
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LD_LIBRARY_PATH",
    "LOCALAPPDATA",
    "PATH",
    "PATHEXT",
    "PLAYWRIGHT_BROWSERS_PATH",
    "PLAYWRIGHT_NODEJS_PATH",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "USERPROFILE",
    "WINDIR",
    "XDG_CACHE_HOME",
)

#: Only these paths exist on the worker-local render origin. The worker has the
#: whole Django application available, but a helper whose one job is to draw a
#: card has no reason to expose login, admin, API, or student-portal routes even
#: on loopback.
_CARD_PATH_PREFIX = "/telegram/card/"
_CARD_ASSET_PATH_PREFIX = "/telegram/card-assets/"

#: A request should already be complete when Playwright closes its page. If one
#: is still unwinding, give it a short bounded grace period; never let a stuck
#: database/static request turn `server_close()` into an unbounded worker hang.
ORIGIN_REQUEST_JOIN_TIMEOUT_SECONDS = 5.0

_CARD_TOKEN_IN_TEXT = re.compile(r"(/telegram/card/)[^/?\s]+")
_ORIGIN_BATCH_LOCK = Lock()
_LINGERING_LOCK = Lock()
_LINGERING_ORIGIN_THREADS: set[Thread] = set()


class _LoopbackWSGIServer(ThreadingMixIn, WSGIServer):
    """A small concurrent WSGI origin reachable only from this machine."""

    daemon_threads = True
    block_on_close = False
    allow_reuse_address = True

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._request_threads: set[Thread] = set()
        self._request_threads_lock = Lock()
        super().__init__(*args, **kwargs)

    def process_request(self, request: Any, client_address: Any) -> None:
        """Start and explicitly track one handler thread.

        `ThreadingMixIn.block_on_close=False` keeps shutdown bounded but normally
        means handler threads are forgotten. Tracking them lets the context wait
        for healthy requests and quarantine a genuinely stuck one so later jobs
        cannot accumulate another listener and another set of threads.
        """

        thread = Thread(
            target=self._tracked_process_request,
            args=(request, client_address),
            name="telegram-card-origin-request",
            daemon=True,
        )
        with self._request_threads_lock:
            self._request_threads.add(thread)
        try:
            thread.start()
        except Exception:
            with self._request_threads_lock:
                self._request_threads.discard(thread)
            raise

    def _tracked_process_request(self, request: Any, client_address: Any) -> None:
        try:
            self.process_request_thread(request, client_address)
        finally:
            with self._request_threads_lock:
                self._request_threads.discard(current_thread())

    def live_request_threads(self) -> list[Thread]:
        with self._request_threads_lock:
            return [thread for thread in self._request_threads if thread.is_alive()]

    def join_request_threads(self, timeout: float) -> list[Thread]:
        """Wait at most `timeout` seconds total and return handlers still alive."""

        deadline = monotonic() + max(0.0, float(timeout))
        for thread in self.live_request_threads():
            thread.join(timeout=max(0.0, deadline - monotonic()))
        return self.live_request_threads()


class _SilentWSGIRequestHandler(WSGIRequestHandler):
    """Do not write signed card URLs to the standard access log."""

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # The default implementation logs the complete request target. A card
        # target contains a short-lived bearer token, so even an otherwise useful
        # access log is the wrong place for it. Rendering logs the HTTP status
        # separately, without the URL, when a request fails.
        return


class _RedactCardRequestLogs(logging.Filter):
    """Preserve request diagnostics while removing the bearer card token."""

    _telegram_card_token_redactor = True

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - logging must never break rendering.
            message = str(record.msg)
        redacted = _CARD_TOKEN_IN_TEXT.sub(r"\1<redacted>", message)
        if redacted != message or record.args:
            # Store the already-formatted, sanitised message. Keeping the old
            # args would apply them twice; keeping `exc_info` preserves the
            # traceback and exception class that explain a render-time 500.
            record.msg = redacted
            record.args = ()
        if record.exc_info and record.exc_text is None:
            # Tracebacks are normally formatted after logger filters run. Cache
            # a redacted version now so an exception message cannot restore the
            # signed token after the message itself has been sanitised.
            try:
                record.exc_text = logging.Formatter().formatException(record.exc_info)
            except Exception:  # noqa: BLE001 - retain the original diagnostic.
                record.exc_text = None
        if isinstance(record.exc_text, str):
            record.exc_text = _CARD_TOKEN_IN_TEXT.sub(r"\1<redacted>", record.exc_text)
        if isinstance(record.stack_info, str):
            record.stack_info = _CARD_TOKEN_IN_TEXT.sub(r"\1<redacted>", record.stack_info)
        return True


def _install_card_request_log_redaction() -> None:
    """Install one permanent redactor on the framework request loggers."""

    for name in ("django.request", "django.server"):
        request_logger = logging.getLogger(name)
        if not any(
            bool(getattr(installed, "_telegram_card_token_redactor", False))
            for installed in request_logger.filters
        ):
            request_logger.addFilter(_RedactCardRequestLogs())


_install_card_request_log_redaction()


def timetable_images_enabled() -> bool:
    """Whether to send timetable pictures. Read at call time, default ON
    (owner decision 2026-08-22); any value other than "true" disables.

    Separate from `TELEGRAM_ADVISOR_ENABLED` on purpose: a picture of a week grid
    is a compact record of where a student is and when, it is stored on Telegram's
    servers under a durable `file_id`, and it is far easier to forward than prose.
    That deserves its own switch and its own decision.
    """
    return bool(getattr(settings, "TELEGRAM_SEND_TIMETABLE_IMAGES", False))


def graduation_images_enabled() -> bool:
    """Whether to export graduation-plan maps to Telegram, default ON
    (owner decision 2026-08-22); any value other than "true" disables.

    This is deliberately independent from timetable images because the map
    contains a substantially broader academic-progress snapshot.
    """

    return bool(getattr(settings, "TELEGRAM_SEND_GRADUATION_IMAGES", False))


def images_enabled() -> bool:
    """Whether the private adviser-card rendering surface is needed at all."""

    return timetable_images_enabled() or graduation_images_enabled()


def presentation_images_enabled(presentation: Any) -> bool:
    """Whether this normalized presentation's own export switch is enabled."""

    if not isinstance(presentation, dict):
        return False
    kind = str(presentation.get("kind") or "")
    if kind == "timetable_proposals":
        return timetable_images_enabled()
    if kind == "graduation_scenario":
        return graduation_images_enabled()
    return False


class CardRenderer(Protocol):
    """What the gateway needs in order to get a PNG. Nothing else is called."""

    def render(self, url: str) -> bytes | None: ...


@dataclass
class RecordingRenderer:
    """Deterministic bytes, and a record of what was asked for. For tests."""

    #: A 1x1 PNG. Small, valid, and obviously not a real card.
    png: bytes = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
        b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    requested: list[str] = field(default_factory=list)
    #: Set to make every render fail, to exercise the degrade-to-text path.
    fail: bool = False

    def render(self, url: str) -> bytes | None:
        self.requested.append(url)
        return None if self.fail else self.png

    def render_many(self, urls: list[str]) -> list[bytes | None]:
        return [self.render(url) for url in urls]


def _validated_signed_card_urls(urls: list[str]) -> list[str] | None:
    """Accept one small batch of genuine card URLs on one IPv4 loopback origin."""

    if not 1 <= len(urls) <= _render_child_protocol.MAX_RENDER_BATCH_URLS:
        return None

    from .cards import unsign_card

    validated: list[str] = []
    origins: set[tuple[str, str, int | None]] = set()
    for value in urls:
        if not isinstance(value, str) or not value or len(value) > 4096:
            return None
        try:
            parsed = urlparse(value)
            host = (parsed.hostname or "").lower()
            port = parsed.port
        except ValueError:
            return None
        path = parsed.path
        if not path.startswith(_CARD_PATH_PREFIX) or not path.endswith("/"):
            return None
        token = path[len(_CARD_PATH_PREFIX) : -1]
        if not (
            parsed.scheme.lower() == "http"
            and host in {"127.0.0.1", "localhost"}
            and parsed.username is None
            and parsed.password is None
            and not parsed.params
            and not parsed.query
            and not parsed.fragment
            and token
            and "/" not in token
            and unsign_card(token) is not None
        ):
            return None
        origins.add((parsed.scheme.lower(), host, port))
        validated.append(value)
    return validated if len(origins) == 1 else None


def _render_child_environment() -> dict[str, str]:
    """Minimal OS/runtime environment; deliberately excludes application secrets."""

    return {name: os.environ[name] for name in _RENDER_CHILD_ENV_NAMES if name in os.environ}


def _terminate_render_child_process_tree(process: subprocess.Popen[bytes]) -> None:
    """Best-effort bounded teardown of Python, Playwright, and Chromium."""

    # Do not return merely because the direct Python child has exited. On Linux
    # its process group can still contain orphaned Playwright/Chromium children;
    # killing that group is precisely the cleanup needed after an abnormal exit.
    direct_child_running = process.poll() is None
    if os.name == "nt":
        system_root = os.environ.get("SYSTEMROOT") or os.environ.get("WINDIR") or r"C:\Windows"
        taskkill = os.path.join(system_root, "System32", "taskkill.exe")
        try:
            subprocess.run(  # noqa: S603 - fixed system binary and numeric PID.
                [taskkill, "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=RENDER_CHILD_KILL_TIMEOUT_SECONDS,
            )
        except Exception:
            if direct_child_running:
                try:
                    process.kill()
                except Exception:
                    pass
    else:
        try:
            # These APIs exist only on POSIX and this branch is unreachable on
            # Windows; Windows-targeted type stubs therefore omit them.
            os.killpg(process.pid, signal.SIGKILL)  # type: ignore[attr-defined]
        except ProcessLookupError:
            pass
        except Exception:
            if direct_child_running:
                try:
                    process.kill()
                except Exception:
                    pass
    try:
        process.wait(timeout=RENDER_CHILD_KILL_TIMEOUT_SECONDS)
    except Exception:
        pass


@dataclass
class _BoundedPipeRead:
    data: bytearray = field(default_factory=bytearray)
    done: Event = field(default_factory=Event)
    too_large: Event = field(default_factory=Event)
    failed: Event = field(default_factory=Event)


@dataclass
class _PipeWrite:
    done: Event = field(default_factory=Event)
    failed: Event = field(default_factory=Event)


def _read_child_stdout(stream: Any, *, limit: int, state: _BoundedPipeRead) -> None:
    try:
        while len(state.data) <= limit:
            chunk = stream.read(min(64 * 1024, (limit + 1) - len(state.data)))
            if not chunk:
                break
            state.data.extend(chunk)
            if len(state.data) > limit:
                state.too_large.set()
                break
    except Exception:  # noqa: BLE001 - only the fixed failure category is logged.
        state.failed.set()
    finally:
        state.done.set()


def _write_child_stdin(stream: Any, payload: bytes, state: _PipeWrite) -> None:
    try:
        stream.write(payload)
        stream.flush()
    except BrokenPipeError:
        # The return code is the useful diagnostic when the child exits early.
        pass
    except Exception:  # noqa: BLE001 - never log input or exception text.
        state.failed.set()
    finally:
        try:
            stream.close()
        except Exception:
            pass
        state.done.set()


def _render_child_output_limit(expected_count: int) -> int:
    framing = len(_render_child_protocol.PROTOCOL_MAGIC) + 1 + (expected_count * 5)
    return framing + (expected_count * _render_child_protocol.MAX_RENDERED_PNG_BYTES)


def _run_render_child(payload: bytes, *, expected_count: int) -> bytes | None:
    """Run the renderer with bounded pipes and a hard parent-enforced deadline."""

    child_script = str(Path(_render_child_protocol.__file__).resolve())
    popen_options: dict[str, Any] = {
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.DEVNULL,
        "cwd": str(Path(child_script).parent),
        "env": _render_child_environment(),
        "close_fds": True,
        "bufsize": 0,
    }
    if os.name == "nt":
        popen_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_options["start_new_session"] = True

    try:
        process = subprocess.Popen(  # noqa: S603 - fixed interpreter and fixed script.
            [sys.executable, "-I", child_script],
            **popen_options,
        )
    except Exception:
        logger.warning("telegram: isolated card renderer could not start")
        return None
    if process.stdin is None or process.stdout is None:
        _terminate_render_child_process_tree(process)
        logger.warning("telegram: isolated card renderer pipes were unavailable")
        return None

    read_state = _BoundedPipeRead()
    write_state = _PipeWrite()
    reader = Thread(
        target=_read_child_stdout,
        kwargs={
            "stream": process.stdout,
            "limit": _render_child_output_limit(expected_count),
            "state": read_state,
        },
        name="telegram-card-render-child-output",
        daemon=True,
    )
    writer = Thread(
        target=_write_child_stdin,
        args=(process.stdin, payload, write_state),
        name="telegram-card-render-child-input",
        daemon=True,
    )
    reader.start()
    writer.start()

    failure = ""
    deadline = monotonic() + max(0.01, float(RENDER_BATCH_TIMEOUT_SECONDS))
    while True:
        if read_state.too_large.is_set():
            failure = "output_limit"
            break
        if read_state.failed.is_set() or write_state.failed.is_set():
            failure = "pipe_failure"
            break
        if process.poll() is not None and read_state.done.is_set() and write_state.done.is_set():
            break
        remaining = deadline - monotonic()
        if remaining <= 0:
            failure = "timeout"
            break
        read_state.done.wait(timeout=min(0.02, remaining))

    if failure:
        _terminate_render_child_process_tree(process)
    reader.join(timeout=RENDER_CHILD_KILL_TIMEOUT_SECONDS)
    writer.join(timeout=RENDER_CHILD_KILL_TIMEOUT_SECONDS)
    try:
        process.stdout.close()
    except Exception:
        pass

    if failure == "timeout":
        logger.warning("telegram: isolated card renderer exceeded its hard deadline")
        return None
    if failure == "output_limit":
        logger.warning("telegram: isolated card renderer exceeded its output limit")
        return None
    if failure:
        logger.warning("telegram: isolated card renderer pipe failed")
        return None
    if process.returncode != 0:
        # The Python driver may have died after spawning Chromium. Its process
        # group can outlive it, so reap the whole tree even though poll() already
        # reports the direct child as exited.
        _terminate_render_child_process_tree(process)
        logger.warning("telegram: isolated card renderer child failed")
        return None
    return bytes(read_state.data)


def _decode_render_child_response(raw: bytes, *, expected_count: int) -> list[bytes | None] | None:
    """Strictly decode the bounded binary response; reject truncation or extras."""

    if not raw or len(raw) > _render_child_output_limit(expected_count):
        return None
    magic = _render_child_protocol.PROTOCOL_MAGIC
    if len(raw) < len(magic) + 1 or raw[: len(magic)] != magic:
        return None
    offset = len(magic)
    count = raw[offset]
    offset += 1
    if count != expected_count:
        return None
    images: list[bytes | None] = []
    for _index in range(count):
        if offset >= len(raw):
            return None
        marker = raw[offset]
        offset += 1
        if marker == 0:
            images.append(None)
            continue
        if marker != 1 or offset + 4 > len(raw):
            return None
        length = struct.unpack_from("!I", raw, offset)[0]
        offset += 4
        if not 1 <= length <= _render_child_protocol.MAX_RENDERED_PNG_BYTES:
            return None
        if offset + length > len(raw):
            return None
        image = raw[offset : offset + length]
        offset += length
        if not image.startswith(b"\x89PNG\r\n\x1a\n"):
            return None
        images.append(image)
    return images if offset == len(raw) else None


class PlaywrightCardRenderer:
    """Render cards in a credential-free, hard-deadline child process."""

    def render(self, url: str) -> bytes | None:
        found = self.render_many([url])
        return found[0] if found else None

    def render_many(self, urls: list[str]) -> list[bytes | None]:
        """Render one answer's cards in an isolated browser process tree.

        The child receives only short-lived signed loopback requests. Its
        allowlisted environment contains no Django secret, database URL,
        Telegram token, or LLM credential. If Python, Playwright, Chromium, or
        browser.close wedges, the parent kills the complete process group and
        the durable delivery path retains its existing text fallback.
        """

        if not urls:
            return []
        try:
            validated = _validated_signed_card_urls(urls)
            if validated is None:
                logger.warning("telegram: isolated card renderer refused its URL batch")
                return []

            from .cards import sign_renderer_request

            request = json.dumps(
                {
                    "version": 1,
                    "urls": validated,
                    "renderer_token": sign_renderer_request(),
                },
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode("utf-8")
            if len(request) > _render_child_protocol.MAX_RENDER_CHILD_INPUT_BYTES:
                logger.warning("telegram: isolated card renderer input exceeded its limit")
                return []

            response = _run_render_child(request, expected_count=len(validated))
            if response is None:
                return []
            images = _decode_render_child_response(response, expected_count=len(validated))
            if images is None:
                logger.warning("telegram: isolated card renderer returned an invalid response")
                return []
            return images
        except Exception as exc:  # noqa: BLE001
            # Never log a URL, child output, environment value, or exception text.
            logger.warning("telegram: isolated card renderer failed (%s)", type(exc).__name__)
            return []


_RENDERER: CardRenderer | None = None


def get_renderer() -> CardRenderer:
    global _RENDERER
    if _RENDERER is None:
        _RENDERER = PlaywrightCardRenderer()
    return _RENDERER


def set_renderer(renderer: CardRenderer | None) -> None:
    """Install a renderer, or `None` to fall back to the real one."""
    global _RENDERER
    _RENDERER = renderer


def render_card(*, message_id: Any, base_url: str, option_index: int | None = None) -> bytes | None:
    """A PNG of this message's adviser card, or `None` — never an exception.

    `base_url` is where the headless browser should reach this server. It is the
    LOCAL origin, not `TELEGRAM_PUBLIC_BASE_URL`: the browser runs beside the
    server, so sending it out through the public hostname would take a signed URL
    on a round trip through the internet for no reason.
    """
    if not images_enabled():
        return None
    if not base_url:
        # Said once, plainly. Without this the caller cannot tell "no picture
        # because it is switched off" from "no picture because nobody knows where
        # this server is".
        logger.warning("telegram: cannot render a card without an internal base URL")
        return None

    from .cards import sign_card

    token = sign_card(message_id=message_id, option_index=option_index)
    url = f"{str(base_url).rstrip('/')}/telegram/card/{token}/"
    try:
        return get_renderer().render(url)
    except Exception as exc:  # noqa: BLE001
        logger.warning("telegram: card renderer raised (%s)", type(exc).__name__)
        return None


def render_cards(
    *, message_id: Any, base_url: str, option_indexes: list[int | None]
) -> list[bytes | None]:
    """Every card for one answer, sharing one browser. Never raises."""
    if not images_enabled():
        return []
    if not base_url:
        logger.warning("telegram: cannot render a card without an internal base URL")
        return []

    from .cards import sign_card

    base = str(base_url).rstrip("/")
    urls = [
        f"{base}/telegram/card/{sign_card(message_id=message_id, option_index=i)}/"
        for i in option_indexes
    ]
    renderer = get_renderer()
    try:
        many = getattr(renderer, "render_many", None)
        if callable(many):
            return list(many(urls))
        return [renderer.render(url) for url in urls]
    except Exception as exc:  # noqa: BLE001
        logger.warning("telegram: card renderer raised (%s)", type(exc).__name__)
        return []


def local_base_url(port: int | str | None = None) -> str:
    """Where the headless browser reaches this process.

    `port` is the port THIS process is listening on, taken from the webhook
    request that triggered the work (`SERVER_PORT`). Deriving it beats defaulting
    it: the first version hard-coded 8000, the dev server runs on 8001/8002, and
    the resulting failure was silent — the renderer fetched a port with nothing on
    it, returned `None`, and the turn degraded to text exactly as it does when
    Chromium is missing. A wrong default that fails the same way as a legitimate
    fallback is a default that hides its own misconfiguration.

    The explicit setting still wins, for deployments where the request's port is
    not where the app is reachable (a unix socket, a container port mapping).
    """
    configured = str(getattr(settings, "TELEGRAM_INTERNAL_BASE_URL", "") or "").strip()
    if configured:
        # Refused if it is not local. The natural workaround for a broken loopback
        # fetch is to point this at the public origin — and that would send a signed
        # card token, a bearer credential for one student's timetable, out across the
        # internet and back through the edge on every render.
        try:
            parsed = urlparse(configured)
            host = (parsed.hostname or "").lower()
            # Accessing `.port` performs urllib's range and integer validation.
            _ = parsed.port
        except ValueError:
            parsed = None
            host = ""
        # IPv4 loopback only. `0.0.0.0` is a bind address, not a destination;
        # `::1` is also refused because the private origin is deliberately bound
        # only to 127.0.0.1. Accepting either would make validation broader than
        # the listener it is meant to describe.
        valid = bool(
            parsed is not None
            and parsed.scheme.lower() == "http"
            and host in {"127.0.0.1", "localhost"}
            and parsed.username is None
            and parsed.password is None
            and parsed.path in {"", "/"}
            and not parsed.params
            and not parsed.query
            and not parsed.fragment
        )
        if not valid:
            logger.error(
                "telegram: TELEGRAM_INTERNAL_BASE_URL must be a plain HTTP "
                "loopback origin without credentials, a path, query, or fragment"
            )
            return ""
        return configured.rstrip("/")
    if port:
        return f"http://127.0.0.1:{int(port)}"
    logger.warning(
        "telegram: no internal base URL and no request port; "
        "set TELEGRAM_INTERNAL_BASE_URL or images will not render"
    )
    return ""


def _card_only_application(application: Any) -> Any:
    """Expose the card page and exact card-asset namespace, nothing else."""

    allowed_prefixes = (_CARD_PATH_PREFIX, _CARD_ASSET_PATH_PREFIX)

    def card_only(environ: dict[str, Any], start_response: Any) -> Any:
        path = str(environ.get("PATH_INFO", "") or "")
        if not any(path.startswith(prefix) for prefix in allowed_prefixes):
            start_response(
                "404 Not Found",
                [
                    ("Content-Length", "0"),
                    ("Cache-Control", "no-store, private"),
                ],
            )
            return [b""]
        return application(environ, start_response)

    return card_only


def _live_lingering_origin_threads() -> list[Thread]:
    """Forget completed quarantined threads and return those still running."""

    with _LINGERING_LOCK:
        live = {thread for thread in _LINGERING_ORIGIN_THREADS if thread.is_alive()}
        _LINGERING_ORIGIN_THREADS.clear()
        _LINGERING_ORIGIN_THREADS.update(live)
        return list(live)


def _remember_lingering_origin_threads(threads: list[Thread]) -> None:
    with _LINGERING_LOCK:
        _LINGERING_ORIGIN_THREADS.update(thread for thread in threads if thread.is_alive())


@contextmanager
def worker_card_origin(port: int | str | None = None) -> Iterator[str]:
    """Yield one local card-rendering origin for a durable delivery batch.

    An explicitly configured loopback origin (or the request port used by the
    legacy in-process path) still wins. A durable worker normally has neither,
    because it is not handling the webhook request and, in production, its own
    loopback cannot reach the separate web service. In that case it starts the
    project's real Django WSGI stack on an ephemeral loopback port. A dedicated
    card-asset route serves only the renderer's allowlisted files, without sending
    the signed URL to a public or cross-service origin.

    The caller keeps this context open for the whole image batch. There is one
    listener per batch, not one per option, and every exit path stops it.
    """

    configured = str(getattr(settings, "TELEGRAM_INTERNAL_BASE_URL", "") or "").strip()
    if configured or port not in (None, ""):
        existing = local_base_url(port)
        if existing:
            yield existing
            return

    # Only one ephemeral origin may exist in this worker at a time. If a previous
    # handler ignored shutdown, quarantine it and fail this image batch to text
    # instead of leaking another server and another group of threads.
    if not _ORIGIN_BATCH_LOCK.acquire(blocking=False):
        raise RuntimeError("another worker card origin is active")
    try:
        if _live_lingering_origin_threads():
            raise RuntimeError("a previous worker card origin is still stopping")

        _install_card_request_log_redaction()
        application = _card_only_application(get_wsgi_application())
        server = make_server(
            "127.0.0.1",
            0,
            application,
            server_class=_LoopbackWSGIServer,
            handler_class=_SilentWSGIRequestHandler,
        )
        thread = Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.05},
            name="telegram-card-render-origin",
            daemon=True,
        )
        started = False
        try:
            thread.start()
            started = True
            yield f"http://127.0.0.1:{int(server.server_port)}"
        finally:
            if started:
                try:
                    server.shutdown()
                except Exception as exc:  # noqa: BLE001 - still close the listener below.
                    logger.warning(
                        "telegram: worker card origin shutdown failed (%s)",
                        type(exc).__name__,
                    )
            try:
                server.server_close()
            except Exception as exc:  # noqa: BLE001 - never mask the delivery outcome.
                logger.warning(
                    "telegram: worker card origin close failed (%s)",
                    type(exc).__name__,
                )
            if started:
                cleanup_deadline = monotonic() + ORIGIN_REQUEST_JOIN_TIMEOUT_SECONDS
                thread.join(timeout=max(0.0, cleanup_deadline - monotonic()))
                lingering: list[Thread] = []
                if thread.is_alive():
                    logger.error("telegram: worker card origin did not stop cleanly")
                    lingering.append(thread)
                active_requests = server.join_request_threads(
                    max(0.0, cleanup_deadline - monotonic())
                )
                if active_requests:
                    logger.error(
                        "telegram: worker card origin left %s request handler(s) active",
                        len(active_requests),
                    )
                    lingering.extend(active_requests)
                _remember_lingering_origin_threads(lingering)
    finally:
        _ORIGIN_BATCH_LOCK.release()


__all__ = [
    "RENDER_TIMEOUT_MS",
    "CardRenderer",
    "PlaywrightCardRenderer",
    "RecordingRenderer",
    "get_renderer",
    "graduation_images_enabled",
    "images_enabled",
    "local_base_url",
    "presentation_images_enabled",
    "render_card",
    "render_cards",
    "set_renderer",
    "timetable_images_enabled",
    "worker_card_origin",
]

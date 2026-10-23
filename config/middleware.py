"""Transport middleware for the whole project.

``TwJsonGZipMiddleware`` compresses the Timetable Workspace's JSON answers
(stage 3, B5; decision D-14 in ``docs/TIMETABLE-WORKSPACE-UI.md`` section 8).
A board read is about 67 KB of JSON and a three-meeting Place read about 1 MB;
both compress more than tenfold, and the app sent them uncompressed.

It is Django's own ``GZipMiddleware`` with a switch and a filter in front.
**The switch** is ``settings.TIMETABLE_WORKSPACE_JSON_GZIP``, default off and
kept off in production (render.yaml): Render documents that its load balancers
already compress app responses with Brotli and gzip, so gzip here would only
repeat that work on the 0.5-CPU host. It is read on every response, so turning
it on or off needs no new middleware chain. With the switch on, a response is
compressed only when **all** of these hold:

* its path is under ``/ops/tw/`` (the workspace API; no HTML page lives there);
* its media type is ``application/json`` (so never an HTML page, an error page
  or a workbook, even under ``/ops/tw/``);
* it is not streaming (a ``FileResponse`` is a stream, and a stream must reach
  the browser as it is written, not when gzip has buffered enough);
* it does not name itself a file (no ``Content-Disposition`` header);
* its body is at least 1 KB (below that the saving is a few hundred bytes, less
  than the headers of the request itself, and the gzip header and the BREACH
  padding can take more than a hundred bytes of it).

Everything else passes through untouched: no ``Content-Encoding`` and no
``Vary: Accept-Encoding``, because its bytes never depend on that header.

The BREACH mitigation is Django's own and is kept: ``GZipMiddleware`` writes a
random-length file name (up to ``max_random_bytes``) into every gzip header, so
the compressed length does not track a secret in the body. This subclass does
not change ``max_random_bytes``.

Off: set ``TIMETABLE_WORKSPACE_JSON_GZIP`` false (the default). Revert: remove
this middleware's one line from ``settings.MIDDLEWARE``.
"""

from __future__ import annotations

from typing import cast

from django.conf import settings
from django.http import HttpRequest, HttpResponse
from django.http.response import HttpResponseBase
from django.middleware.gzip import GZipMiddleware


class TwJsonGZipMiddleware(GZipMiddleware):
    """``GZipMiddleware`` for the workspace API's JSON only (D-14), behind a switch."""

    path_prefix = "/ops/tw/"
    media_type = "application/json"
    # At least 1 KB (1,024 bytes) of body before it is worth compressing.
    min_length = 1024

    def process_response(
        self, request: HttpRequest, response: HttpResponseBase
    ) -> HttpResponseBase:
        # Off (the default, and production's setting): the response leaves as it
        # came, before the parent can add ``Vary: Accept-Encoding``.
        if not settings.TIMETABLE_WORKSPACE_JSON_GZIP:
            return response
        if not self.should_compress(request, response):
            return response
        return super().process_response(request, response)

    def should_compress(self, request: HttpRequest, response: HttpResponseBase) -> bool:
        """True when ``response`` is workspace JSON worth compressing.

        Whether the client accepts gzip is not asked here: the parent class
        asks it, after it has added ``Vary: Accept-Encoding``, so a cache keeps
        the two encodings of an eligible answer apart.
        """
        if not request.path_info.startswith(self.path_prefix):
            return False
        # A stream (``FileResponse`` among them) is never compressed.
        if response.streaming:
            return False
        # A response that names itself a file is a download, never compressed.
        if response.has_header("Content-Disposition"):
            return False
        content_type = response.get("Content-Type", "")
        if content_type.split(";", 1)[0].strip().lower() != self.media_type:
            return False
        # Not a stream, so the whole body is in hand.
        return len(cast(HttpResponse, response).content) >= self.min_length

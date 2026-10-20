"""Request lanes: heavy read-only previews never take every request thread.

Production runs one gunicorn worker with four gthread threads behind one FIFO
queue on half a CPU (``render.yaml``). On 2026-10-20 one user's split-workspace
clicks filled all four threads with CPU-bound previews (room candidates, slot
and student-outcome candidates); ``/health/`` queued behind them past Render's
5 s probe and Render restarted the only instance.

A heavy preview therefore takes a token from a small process-wide lane, without
waiting. When the lane is full it is answered at once with 503
``SERVER_BUSY`` and ``Retry-After``; the page retries its newest request after
that pause. ``PREVIEW_LANE_SIZE`` is ``--threads`` minus one for the health
check minus one for a user-paced write (a move, a create), so neither can be
starved by previews. Writes and ``/health/`` never take the lane.

Like ``job_runtime``'s solver slot, the lane is per process: it holds because
the service runs exactly one worker.
"""

from __future__ import annotations

import functools
import logging
import threading

from django.http import JsonResponse

logger = logging.getLogger(__name__)

#: gunicorn --threads (render.yaml, Procfile) is 4: two for previews, one kept
#: for /health/, one for a write. tests/test_tw_preview_lane.py pins
#: ``PREVIEW_LANE_SIZE <= threads - 2`` against both start commands.
PREVIEW_LANE_SIZE = 2
PREVIEW_RETRY_AFTER_SECONDS = 2
SERVER_BUSY = "SERVER_BUSY"
#: Set on every view the lane guards (structural pin in the tests).
LANE_MARKER = "preview_lane"

_PREVIEW_LANE = threading.BoundedSemaphore(PREVIEW_LANE_SIZE)


def server_busy_response(
    message: str = "The server is busy with other previews. Retrying shortly.",
    *,
    retry_after: int = PREVIEW_RETRY_AFTER_SECONDS,
    details: dict | None = None,
) -> JsonResponse:
    """503 ``SERVER_BUSY``: answered at once, never queued."""
    error: dict[str, object] = {"code": SERVER_BUSY, "message": message}
    if details:
        error["details"] = details
    response = JsonResponse({"ok": False, "code": SERVER_BUSY, "error": error}, status=503)
    response["Retry-After"] = str(retry_after)
    return response


def preview_lane(view):
    """Run ``view`` holding a preview-lane token, or answer 503 at once.

    The token is released when the view returns or raises. Put it below
    ``login_required`` and the method check, so a refused or anonymous request
    never takes a token.
    """

    @functools.wraps(view)
    def wrapped(request, *args, **kwargs):
        if not _PREVIEW_LANE.acquire(blocking=False):
            logger.warning(
                "preview lane full (%s of %s): %s %s refused with 503 SERVER_BUSY",
                PREVIEW_LANE_SIZE,
                PREVIEW_LANE_SIZE,
                request.method,
                request.path,
            )
            return server_busy_response()
        try:
            return view(request, *args, **kwargs)
        finally:
            _PREVIEW_LANE.release()

    setattr(wrapped, LANE_MARKER, True)
    return wrapped

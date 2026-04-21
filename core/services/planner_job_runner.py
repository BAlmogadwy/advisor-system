"""PR7 commit 2 — planner job runner skeleton.

Thin orchestration layer around ``PlannerJob`` rows. Commit 2 lands
only the submit / get / flag helpers; the execution path, cooperative
cancellation, and API views arrive in commits 3–5.

This is an **async UX shim**, not a distributed job system. See
``docs/PR7-DOR.md`` §"What PR7 is NOT" for the scope floor
(process-local, not durable across restarts, cooperative cancel only).
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

from django.conf import settings

from core.models import PlannerJob

ASYNC_PLANNER_ENABLED_SETTING = "TIMETABLE_PR7_ASYNC_PLANNER_ENABLED"


def is_async_planner_enabled() -> bool:
    """Return whether the PR7 async planner is active.

    Reads ``settings.TIMETABLE_PR7_ASYNC_PLANNER_ENABLED``. Default
    ``False`` until commit 8 (promotion). Env override
    ``TIMETABLE_PR7_ASYNC_PLANNER_ENABLED=false`` is the live
    kill-switch once promoted.
    """
    return bool(getattr(settings, ASYNC_PLANNER_ENABLED_SETTING, False))


def _compute_request_signature(scenario_id: int, mode: str) -> str:
    raw = f"{scenario_id}|{mode}".encode()
    return hashlib.sha256(raw).hexdigest()[:32]


def submit_planner_job(
    *,
    scenario_id: int,
    mode: str,
    user: Any | None = None,
    board_id: int | None = None,
) -> uuid.UUID:
    """Create a ``PlannerJob`` row in ``queued`` and return its id.

    Row-creation only at commit 2 — execution is wired at commit 3.
    """
    job = PlannerJob.objects.create(
        id=uuid.uuid4(),
        scenario_id=scenario_id,
        board_id=board_id,
        mode=mode,
        status=PlannerJob.STATUS_QUEUED,
        submitted_by=user if getattr(user, "is_authenticated", False) else None,
        request_signature=_compute_request_signature(scenario_id, mode),
    )
    return job.id


def get_planner_job(job_id: uuid.UUID | str) -> PlannerJob | None:
    return PlannerJob.objects.filter(id=job_id).first()


__all__ = [
    "ASYNC_PLANNER_ENABLED_SETTING",
    "get_planner_job",
    "is_async_planner_enabled",
    "submit_planner_job",
]

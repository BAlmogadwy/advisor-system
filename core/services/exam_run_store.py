"""Save changes rewrites the opened exam timetable in place.

Owner decision, 2026-11-18: each Save of changes used to add another run with
the same name, so Saved timetables filled with versions of one timetable. A Save
now overwrites the run it was made on. Build, Optimize, Fix, Add courses and
Copy still save new runs.

Overwriting loses the previous board, so the write is guarded:

* The page names the run AND the revision it was made on. A run saved again
  since - by a colleague, or by this registrar in another tab - has a higher
  revision, and the Save is refused (``SaveTargetChanged``) rather than
  silently undoing the other save.
* A run deleted since is refused (``SaveTargetGone``); nothing is recreated.
* Each overwrite is put on the audit record with the checksum of the board it
  replaced, the one trace left of it.

``overwrite_run`` must run inside the caller's transaction, so a background
job's run and its SUCCEEDED row still commit together or not at all.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Any

from django.db.models import F
from django.utils import timezone

from core.models import ExamTimetableRun
from core.services.audit import AuditUnavailable, record_audit_event

logger = logging.getLogger(__name__)

OVERWRITE_ACTION = "exam_timetable.overwrite_run"


class SaveTargetChanged(ValueError):
    """The opened run was saved again after the page opened it."""

    code = "save_target_changed"


class SaveTargetGone(ValueError):
    """The opened run was deleted after the page opened it."""

    code = "save_target_gone"


@dataclass(frozen=True)
class RunTarget:
    """The run a Save rewrites, and the revision the page made its changes on."""

    run_id: int
    revision: int


@dataclass(frozen=True)
class AuditActor:
    """Who asked for a Save, and through which endpoint, for its audit row."""

    username: str = ""
    role: str = ""
    endpoint: str = ""
    method: str = ""


@dataclass(frozen=True)
class Overwrite:
    """What one in-place Save replaced: enough for the audit record."""

    run_id: int
    label: str
    previous_label: str
    revision: int
    previous_revision: int
    result_sha256: str
    previous_result_sha256: str


def _whole(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def save_target(payload: dict) -> RunTarget:
    """The run a Save changes request rewrites, from what the page sent.

    A page from before this release sends no revision: it is refused, since it
    cannot say which version its changes were made on.
    """
    run_id = _whole(payload.get("previous_run_id"))
    revision = _whole(payload.get("run_revision"))
    if run_id is None or revision is None:
        raise SaveTargetChanged(
            "This page does not know which version of the timetable it changed. "
            "Open the timetable again from Saved timetables, then make your changes again."
        )
    return RunTarget(run_id=run_id, revision=revision)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def overwrite_run(target: RunTarget, *, label: str, result_json: str) -> Overwrite:
    """Rewrite ``target`` with a new board, or raise; call inside a transaction.

    The update is conditional on the revision, so two Saves made on the same
    version cannot both land: the second finds the revision moved and is
    refused, whichever process it runs in.
    """
    previous = (
        ExamTimetableRun.objects.select_for_update()
        .filter(pk=target.run_id)
        .values("label", "revision", "result_json")
        .first()
    )
    if previous is None:
        raise SaveTargetGone(
            "This timetable was deleted from Saved timetables after you opened it. "
            "Nothing was saved."
        )
    written = ExamTimetableRun.objects.filter(pk=target.run_id, revision=target.revision).update(
        label=label,
        result_json=result_json,
        revision=F("revision") + 1,
        saved_at=timezone.now(),
    )
    if not written:
        raise SaveTargetChanged(
            "This timetable was saved again after you opened it, so your changes were "
            "not saved over it. Open it again from Saved timetables to see the latest "
            "version, then make your changes again."
        )
    return Overwrite(
        run_id=target.run_id,
        label=label,
        previous_label=previous["label"],
        revision=target.revision + 1,
        previous_revision=previous["revision"],
        result_sha256=_sha256(result_json),
        previous_result_sha256=_sha256(previous["result_json"]),
    )


def audit_overwrite(record: Overwrite, actor: AuditActor, *, job_id: str | None = None) -> None:
    """Put a committed overwrite on the audit record. Call after the commit.

    Best effort, as for Copy and Delete: the board is already saved, and a
    registrar's Save must not be reported as failed because the audit log was.
    """
    details: dict[str, Any] = {
        "run_id": record.run_id,
        "label": record.label,
        "previous_label": record.previous_label,
        "revision": record.revision,
        "previous_revision": record.previous_revision,
        "result_sha256": record.result_sha256,
        "previous_result_sha256": record.previous_result_sha256,
    }
    if job_id:
        details["job_id"] = job_id
    try:
        record_audit_event(
            actor_username=actor.username,
            actor_role=actor.role,
            action=OVERWRITE_ACTION,
            endpoint=actor.endpoint,
            method=actor.method,
            status="success",
            details=details,
        )
    except AuditUnavailable:
        logger.error("exam run %s overwritten but not audited", record.run_id)

import csv
import hashlib
import hmac
import json
import logging
from datetime import UTC, datetime, timedelta
from io import StringIO
from threading import Lock
from typing import Any

from django.conf import settings
from django.db import OperationalError, transaction
from django.http import HttpRequest

from core.models import AuditLog

logger = logging.getLogger(__name__)
_AUDIT_WRITE_LOCK = Lock()


def ensure_audit_schema() -> None:
    # Schema is managed by Django migrations (core/migrations/0001_core_scope_and_audit.py).
    # Keep this function as a compatibility no-op for existing call sites.
    return


def _compute_entry_hash(
    *,
    ts_utc: str,
    actor_username: str,
    actor_role: str,
    action: str,
    endpoint: str,
    method: str,
    status: str,
    details_json: str,
    error_text: str,
    prev_hash: str,
) -> str:
    # NOTE: Changed from plain SHA-256 to HMAC-SHA-256 for tamper resistance.
    # Existing entries created before this change will fail chain validation
    # because their hashes were computed with plain SHA-256. This is acceptable
    # since the chain validates forward from the first entry; new entries use HMAC.
    canonical = "|".join(
        [
            prev_hash,
            ts_utc,
            actor_username,
            actor_role,
            action,
            endpoint,
            method,
            status,
            details_json,
            error_text,
        ]
    )
    return hmac.new(
        settings.SECRET_KEY.encode("utf-8"),
        canonical.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _compute_legacy_entry_hash(
    *,
    ts_utc: str,
    actor_username: str,
    actor_role: str,
    action: str,
    endpoint: str,
    method: str,
    status: str,
    details_json: str,
    error_text: str,
    prev_hash: str,
) -> str:
    canonical = "|".join(
        [
            prev_hash,
            ts_utc,
            actor_username,
            actor_role,
            action,
            endpoint,
            method,
            status,
            details_json,
            error_text,
        ]
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _parse_ts(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except Exception:
        return None


def _emit_security_alert(
    request: HttpRequest | None, *, rule: str, details: dict[str, Any]
) -> None:
    try:
        _append_audit_row(
            actor_username="system",
            actor_role="SYSTEM",
            action="security.alert",
            endpoint=request.path if request is not None else "",
            method=str(request.method) if request is not None else "",
            status="critical",
            details_json=json.dumps({"rule": rule, **details}, ensure_ascii=False),
            error_text="",
        )
    except Exception:
        return


def _check_critical_alerts(
    request: HttpRequest,
    *,
    actor_username: str,
    action: str,
    status: str,
    details: dict[str, Any] | None,
) -> None:
    if action == "security.alert":
        return

    # 1) repeated failed admin mutations
    if status == "error" and (action.startswith("db.") or action.startswith("advisor.")):
        try:
            cutoff = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
            count = (
                AuditLog.objects.filter(
                    ts_utc__gte=cutoff,
                    actor_username=actor_username,
                    status="error",
                )
                .filter(
                    models_Q_action_db_or_advisor(),
                )
                .count()
            )
            if count >= 3:
                _emit_security_alert(
                    request,
                    rule="REPEATED_FAILED_ADMIN_MUTATIONS",
                    details={
                        "actor_username": actor_username,
                        "window_minutes": 10,
                        "count": count,
                    },
                )
        except Exception:
            logger.warning("Security alert check failed", exc_info=True)

    # 2) unusual bulk assignment patterns
    if action == "advisor.assign_students" and status == "success":
        d = details or {}
        updated = int(d.get("updated", 0) or 0)
        received = int(d.get("received", 0) or 0)
        if updated >= 200 or received >= 500:
            _emit_security_alert(
                request,
                rule="UNUSUAL_BULK_ASSIGNMENT",
                details={
                    "actor_username": actor_username,
                    "updated": updated,
                    "received": received,
                },
            )


def models_Q_action_db_or_advisor() -> object:
    from django.db.models import Q

    return Q(action__startswith="db.") | Q(action__startswith="advisor.")


class AuditUnavailable(RuntimeError):
    """The audit row could not be written, so the audited action must not happen."""


def _append_audit_row(
    *,
    actor_username: str,
    actor_role: str,
    action: str,
    endpoint: str,
    method: str,
    status: str,
    details_json: str,
    error_text: str,
) -> str:
    """Append one row to the HMAC chain and return its ``entry_hash``.

    The process lock and ``select_for_update`` on the last row serialise every
    writer, so two rows can never claim the same predecessor. Raises whatever
    the database raises; callers decide whether that is fatal.
    """
    ts_utc = datetime.now(UTC).isoformat()
    with _AUDIT_WRITE_LOCK:
        with transaction.atomic():
            last = (
                AuditLog.objects.select_for_update()
                .order_by("-id")
                .values_list("entry_hash", flat=True)
                .first()
            )
            prev_hash = str(last) if last else "GENESIS"
            entry_hash = _compute_entry_hash(
                ts_utc=ts_utc,
                actor_username=actor_username,
                actor_role=actor_role,
                action=action,
                endpoint=endpoint,
                method=method,
                status=status,
                details_json=details_json,
                error_text=error_text,
                prev_hash=prev_hash,
            )
            AuditLog.objects.create(
                ts_utc=ts_utc,
                actor_username=actor_username,
                actor_role=actor_role,
                action=action,
                endpoint=endpoint,
                method=method,
                status=status,
                details_json=details_json,
                error_text=error_text,
                prev_hash=prev_hash,
                entry_hash=entry_hash,
            )
    return entry_hash


def audit_actor(request: HttpRequest) -> tuple[str, str]:
    """The (username, role) an audit row records for this request's user."""
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return "", ""
    groups = list(user.groups.values_list("name", flat=True))
    return user.username, groups[0] if groups else ("SUPER_ADMIN" if user.is_superuser else "")


def record_audit_event(
    *,
    actor_username: str,
    actor_role: str,
    action: str,
    endpoint: str,
    method: str,
    status: str,
    details: dict[str, Any] | None = None,
    error_text: str = "",
) -> str:
    """Write an audit row or raise ``AuditUnavailable``: the fail-closed variant.

    Use it where an action may only happen once it is on the record - a file of
    student data, for example, is only rendered after its row exists, and its
    reference derives from the returned ``entry_hash``. The actor is explicit so
    a background thread can record the user who asked for the work. A locked
    database is retried once; any other failure, or a second lock, raises.
    """
    try:
        details_json = json.dumps(details or {}, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise AuditUnavailable("The audit details could not be recorded.") from exc
    for attempt in (1, 2):
        try:
            return _append_audit_row(
                actor_username=actor_username,
                actor_role=actor_role,
                action=action,
                endpoint=endpoint,
                method=method,
                status=status,
                details_json=details_json,
                error_text=error_text[:500],
            )
        except OperationalError as exc:
            if attempt == 1 and "locked" in str(exc).lower():
                logger.warning("Audit write hit a locked database; retrying once")
                continue
            logger.error("Strict audit write failed", exc_info=True)
            raise AuditUnavailable("The audit log is unavailable.") from exc
        except Exception as exc:
            logger.error("Strict audit write failed", exc_info=True)
            raise AuditUnavailable("The audit log is unavailable.") from exc
    raise AuditUnavailable("The audit log is unavailable.")  # pragma: no cover


def log_audit_event(
    request: HttpRequest,
    *,
    action: str,
    status: str,
    details: dict[str, Any] | None = None,
    error_text: str = "",
) -> None:
    """Record an audit row; never raises (audit must not break business endpoints)."""
    try:
        actor_username, actor_role = audit_actor(request)
        _append_audit_row(
            actor_username=actor_username,
            actor_role=actor_role,
            action=action,
            endpoint=request.path,
            method=str(request.method),
            status=status,
            details_json=json.dumps(details or {}, ensure_ascii=False),
            error_text=error_text[:500],
        )
        _check_critical_alerts(
            request,
            actor_username=actor_username,
            action=action,
            status=status,
            details=details,
        )
    except Exception:
        # audit must never break business endpoints
        return


def query_audit_logs(
    *,
    action: str | None = None,
    actor_username: str | None = None,
    status: str | None = None,
    from_utc: str | None = None,
    to_utc: str | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    qs = AuditLog.objects.all()
    if action:
        qs = qs.filter(action=action)
    if actor_username:
        qs = qs.filter(actor_username=actor_username)
    if status:
        qs = qs.filter(status=status)
    if from_utc:
        qs = qs.filter(ts_utc__gte=from_utc)
    if to_utc:
        qs = qs.filter(ts_utc__lte=to_utc)

    clamped = max(1, min(limit, 5000))
    rows = qs.order_by("-id")[:clamped]

    items: list[dict[str, Any]] = []
    for r in rows:
        details_json = str(r.details_json or "{}")
        try:
            details = json.loads(details_json)
        except Exception:
            details = {}
        items.append(
            {
                "id": r.id,
                "ts_utc": str(r.ts_utc or ""),
                "actor_username": str(r.actor_username or ""),
                "actor_role": str(r.actor_role or ""),
                "action": str(r.action or ""),
                "endpoint": str(r.endpoint or ""),
                "method": str(r.method or ""),
                "status": str(r.status or ""),
                "details": details,
                "error_text": str(r.error_text or ""),
                "prev_hash": str(r.prev_hash or ""),
                "entry_hash": str(r.entry_hash or ""),
            }
        )
    return items


def export_audit_logs_csv(rows: list[dict[str, Any]]) -> str:
    out = StringIO()
    writer = csv.writer(out)
    writer.writerow(
        [
            "id",
            "ts_utc",
            "actor_username",
            "actor_role",
            "action",
            "endpoint",
            "method",
            "status",
            "reason_code",
            "details_json",
            "error_text",
            "prev_hash",
            "entry_hash",
        ]
    )
    for row in rows:
        details = row.get("details") or {}
        writer.writerow(
            [
                row.get("id", ""),
                row.get("ts_utc", ""),
                row.get("actor_username", ""),
                row.get("actor_role", ""),
                row.get("action", ""),
                row.get("endpoint", ""),
                row.get("method", ""),
                row.get("status", ""),
                details.get("reason_code", ""),
                json.dumps(details, ensure_ascii=False),
                row.get("error_text", ""),
                row.get("prev_hash", ""),
                row.get("entry_hash", ""),
            ]
        )
    return out.getvalue()


def validate_hash_chain(limit: int = 2000) -> dict[str, Any]:
    clamped = max(1, min(limit, 10000))
    rows = AuditLog.objects.order_by("id")[:clamped]

    prev_expected = "GENESIS"
    invalid_ids: list[int] = []
    legacy_ids: list[int] = []
    hmac_ids: list[int] = []
    legacy_allowed = True
    checked = 0
    for r in rows:
        checked += 1
        prev_hash = str(r.prev_hash or "")
        entry_hash = str(r.entry_hash or "")
        recomputed_hmac = _compute_entry_hash(
            ts_utc=str(r.ts_utc or ""),
            actor_username=str(r.actor_username or ""),
            actor_role=str(r.actor_role or ""),
            action=str(r.action or ""),
            endpoint=str(r.endpoint or ""),
            method=str(r.method or ""),
            status=str(r.status or ""),
            details_json=str(r.details_json or "{}"),
            error_text=str(r.error_text or ""),
            prev_hash=prev_hash,
        )
        recomputed_legacy = _compute_legacy_entry_hash(
            ts_utc=str(r.ts_utc or ""),
            actor_username=str(r.actor_username or ""),
            actor_role=str(r.actor_role or ""),
            action=str(r.action or ""),
            endpoint=str(r.endpoint or ""),
            method=str(r.method or ""),
            status=str(r.status or ""),
            details_json=str(r.details_json or "{}"),
            error_text=str(r.error_text or ""),
            prev_hash=prev_hash,
        )

        if entry_hash == recomputed_hmac:
            hmac_ids.append(r.id)
            legacy_allowed = False
        elif legacy_allowed and entry_hash == recomputed_legacy:
            legacy_ids.append(r.id)
        else:
            invalid_ids.append(r.id)

        if prev_hash != prev_expected:
            invalid_ids.append(r.id)
        prev_expected = entry_hash

    return {
        "checked": checked,
        "ok": len(invalid_ids) == 0,
        "invalid_ids": invalid_ids[:100],
        "legacy_count": len(legacy_ids),
        "hmac_count": len(hmac_ids),
    }

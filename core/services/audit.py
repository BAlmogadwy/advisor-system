import csv
import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta
from io import StringIO
from typing import Any

from django.http import HttpRequest

from core.models import AuditLog

logger = logging.getLogger(__name__)


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
        ts_utc = datetime.now(UTC).isoformat()
        actor_username = "system"
        actor_role = "SYSTEM"
        action = "security.alert"
        endpoint = request.path if request is not None else ""
        method = str(request.method) if request is not None else ""
        status = "critical"
        details_json = json.dumps({"rule": rule, **details}, ensure_ascii=False)
        error_text = ""

        last = AuditLog.objects.order_by("-id").values_list("entry_hash", flat=True).first()
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


def log_audit_event(
    request: HttpRequest,
    *,
    action: str,
    status: str,
    details: dict[str, Any] | None = None,
    error_text: str = "",
) -> None:
    try:
        actor_username = (
            request.user.username
            if getattr(request, "user", None) and request.user.is_authenticated
            else ""
        )
        actor_role = ""
        if getattr(request, "user", None) and request.user.is_authenticated:
            groups = list(request.user.groups.values_list("name", flat=True))
            actor_role = (
                groups[0] if groups else ("SUPER_ADMIN" if request.user.is_superuser else "")
            )

        ts_utc = datetime.now(UTC).isoformat()
        endpoint = request.path
        method = str(request.method)
        details_json = json.dumps(details or {}, ensure_ascii=False)
        error_trimmed = error_text[:500]

        last = AuditLog.objects.order_by("-id").values_list("entry_hash", flat=True).first()
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
            error_text=error_trimmed,
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
            error_text=error_trimmed,
            prev_hash=prev_hash,
            entry_hash=entry_hash,
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
    checked = 0
    for r in rows:
        checked += 1
        prev_hash = str(r.prev_hash or "")
        entry_hash = str(r.entry_hash or "")
        recomputed = _compute_entry_hash(
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
        if prev_hash != prev_expected or entry_hash != recomputed:
            invalid_ids.append(r.id)
        prev_expected = entry_hash

    return {"checked": checked, "ok": len(invalid_ids) == 0, "invalid_ids": invalid_ids[:100]}

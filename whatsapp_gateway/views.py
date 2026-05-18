import json
from typing import Any

from django.conf import settings
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from whatsapp_gateway.client import WhatsAppClientError, send_whatsapp_text
from whatsapp_gateway.models import WhatsAppMessageLog
from whatsapp_gateway.services import (
    extract_text_messages,
    process_inbound_text,
    verify_meta_signature,
)


def _json_body(request: HttpRequest) -> tuple[dict[str, Any] | None, JsonResponse | None]:
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, JsonResponse({"error": "Invalid JSON body"}, status=400)
    if not isinstance(payload, dict):
        return None, JsonResponse({"error": "JSON body must be an object"}, status=400)
    return payload, None


@csrf_exempt
@require_http_methods(["GET", "POST"])
def whatsapp_webhook_view(request: HttpRequest) -> HttpResponse:
    if request.method == "GET":
        mode = request.GET.get("hub.mode", "")
        verify_token = request.GET.get("hub.verify_token", "")
        challenge = request.GET.get("hub.challenge", "")
        expected = str(getattr(settings, "WHATSAPP_VERIFY_TOKEN", "") or "")
        if mode == "subscribe" and expected and verify_token == expected:
            return HttpResponse(challenge, content_type="text/plain")
        return JsonResponse({"error": "Webhook verification failed"}, status=403)

    signature = request.headers.get("X-Hub-Signature-256", "")
    if not verify_meta_signature(body=request.body, signature_header=signature):
        return JsonResponse({"error": "Invalid webhook signature"}, status=403)

    payload, err = _json_body(request)
    if err:
        return err
    assert payload is not None

    processed: list[dict[str, Any]] = []
    for message in extract_text_messages(payload):
        result = process_inbound_text(
            wa_id=message["wa_id"],
            phone_number=message.get("phone_number", ""),
            text=message["text"],
        )
        outbound = {"ok": False, "skipped": True}
        reply = str(result.get("reply") or "").strip()
        if reply:
            try:
                outbound = send_whatsapp_text(to_wa_id=message["wa_id"], text=reply)
                WhatsAppMessageLog.objects.create(
                    wa_id=message["wa_id"],
                    direction=WhatsAppMessageLog.DIRECTION_OUTBOUND,
                    message_type="text",
                    text_preview=reply[:500],
                    status="sent" if outbound.get("ok") else "skipped",
                )
            except WhatsAppClientError as exc:
                outbound = {"ok": False, "error": str(exc)}
                WhatsAppMessageLog.objects.create(
                    wa_id=message["wa_id"],
                    direction=WhatsAppMessageLog.DIRECTION_OUTBOUND,
                    message_type="text",
                    text_preview=reply[:500],
                    status="error",
                )
        processed.append(
            {
                "wa_id": message["wa_id"],
                "message_id": message.get("message_id", ""),
                "action": result.get("action"),
                "outbound": outbound,
            }
        )

    return JsonResponse({"ok": True, "processed": processed})

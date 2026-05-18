import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from django.conf import settings


class WhatsAppClientError(RuntimeError):
    """Raised when an outbound WhatsApp message cannot be sent."""


def send_whatsapp_text(*, to_wa_id: str, text: str) -> dict[str, Any]:
    token = str(getattr(settings, "WHATSAPP_ACCESS_TOKEN", "") or "").strip()
    phone_number_id = str(getattr(settings, "WHATSAPP_PHONE_NUMBER_ID", "") or "").strip()
    version = str(getattr(settings, "WHATSAPP_CLOUD_API_VERSION", "v23.0") or "v23.0").strip()
    if not token or not phone_number_id:
        return {"ok": False, "skipped": True, "reason": "whatsapp_not_configured"}

    payload = {
        "messaging_product": "whatsapp",
        "to": to_wa_id,
        "type": "text",
        "text": {"preview_url": False, "body": text[:4000]},
    }
    body = json.dumps(payload).encode("utf-8")
    request = Request(
        f"https://graph.facebook.com/{version}/{phone_number_id}/messages",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:  # noqa: S310
            data = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise WhatsAppClientError(f"WhatsApp Cloud API returned HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise WhatsAppClientError(f"WhatsApp Cloud API is not reachable: {exc.reason}") from exc
    except TimeoutError as exc:
        raise WhatsAppClientError("WhatsApp Cloud API request timed out.") from exc

    return {"ok": True, "response": data}

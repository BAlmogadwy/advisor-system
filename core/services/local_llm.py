import json
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from django.conf import settings


class LocalLLMError(RuntimeError):
    """Base error for local LLM integration failures."""


class LocalLLMConfigError(LocalLLMError):
    """Raised when the configured local LLM endpoint is unsafe or invalid."""


class LocalLLMUnavailable(LocalLLMError):
    """Raised when the local LLM server cannot be reached."""


@dataclass(frozen=True)
class ChatResult:
    content: str
    model: str
    usage: dict[str, Any]
    raw: dict[str, Any]


_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _configured_base_url() -> str:
    base_url = str(getattr(settings, "LOCAL_LLM_BASE_URL", "http://localhost:1234/v1")).strip()
    if not base_url:
        raise LocalLLMConfigError("LOCAL_LLM_BASE_URL is empty.")

    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise LocalLLMConfigError("LOCAL_LLM_BASE_URL must be an absolute http(s) URL.")

    host = (parsed.hostname or "").lower()
    allow_remote = bool(getattr(settings, "LOCAL_LLM_ALLOW_REMOTE", False))
    if host not in _LOCAL_HOSTS and not allow_remote:
        raise LocalLLMConfigError("Local advisor only permits localhost LLM endpoints.")

    return base_url.rstrip("/")


class LocalLLMClient:
    """Small OpenAI-compatible client for LM Studio, Ollama, or llama.cpp servers."""

    def __init__(self, base_url: str | None = None, timeout_seconds: float | None = None) -> None:
        self.base_url = base_url.rstrip("/") if base_url else _configured_base_url()
        self.timeout_seconds = float(
            timeout_seconds
            if timeout_seconds is not None
            else getattr(settings, "LOCAL_LLM_TIMEOUT_SECONDS", 120)
        )

    def _request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = Request(
            f"{self.base_url}{path}",
            data=body,
            method=method,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:  # noqa: S310
                text = response.read().decode("utf-8")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise LocalLLMUnavailable(f"Local LLM returned HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise LocalLLMUnavailable(f"Local LLM is not reachable: {exc.reason}") from exc
        except TimeoutError as exc:
            raise LocalLLMUnavailable("Local LLM request timed out.") from exc

        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise LocalLLMUnavailable("Local LLM returned invalid JSON.") from exc

    def list_models(self) -> list[dict[str, Any]]:
        data = self._request("GET", "/models")
        models = data.get("data", [])
        return [m for m in models if isinstance(m, dict)]

    def resolve_model(self, requested_model: str | None = None) -> str:
        candidate = (requested_model or str(getattr(settings, "LOCAL_LLM_MODEL", ""))).strip()
        if candidate:
            return candidate

        models = self.list_models()
        for item in models:
            model_id = str(item.get("id", "")).strip()
            if model_id:
                return model_id
        raise LocalLLMUnavailable("No model is loaded in the local LLM server.")

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        assistant_prefill: str | None = None,
    ) -> ChatResult:
        resolved_model = self.resolve_model(model)
        request_messages = list(messages)
        if assistant_prefill:
            request_messages.append({"role": "assistant", "content": assistant_prefill})
        payload: dict[str, Any] = {
            "model": resolved_model,
            "messages": request_messages,
            "temperature": temperature,
            "max_tokens": int(max_tokens or getattr(settings, "LOCAL_LLM_MAX_TOKENS", 900)),
        }
        data = self._request("POST", "/chat/completions", payload)
        choices = data.get("choices") or []
        first = choices[0] if choices and isinstance(choices[0], dict) else {}
        message = first.get("message") if isinstance(first.get("message"), dict) else {}
        content = str(message.get("content", "")).strip()
        if not content:
            reasoning = str(message.get("reasoning_content", "")).strip()
            finish_reason = str(first.get("finish_reason", "")).strip()
            if reasoning and finish_reason == "length":
                raise LocalLLMUnavailable(
                    "Local LLM used the full token budget for hidden reasoning before a final "
                    "answer. Increase LOCAL_LLM_MAX_TOKENS or choose a non-thinking model."
                )
            if reasoning:
                raise LocalLLMUnavailable(
                    "Local LLM returned hidden reasoning but no final answer. Choose a model "
                    "that emits message.content for advisor responses."
                )
            raise LocalLLMUnavailable("Local LLM returned an empty response.")

        return ChatResult(
            content=content,
            model=str(data.get("model") or resolved_model),
            usage=data.get("usage") if isinstance(data.get("usage"), dict) else {},
            raw=data,
        )


def check_local_llm_health() -> dict[str, Any]:
    try:
        client = LocalLLMClient()
        models = client.list_models()
        return {
            "ok": True,
            "base_url": client.base_url,
            "default_model": str(getattr(settings, "LOCAL_LLM_MODEL", "")).strip(),
            "models": [
                {"id": str(m.get("id", "")).strip(), "object": str(m.get("object", ""))}
                for m in models
                if str(m.get("id", "")).strip()
            ],
        }
    except LocalLLMError as exc:
        return {
            "ok": False,
            "base_url": str(getattr(settings, "LOCAL_LLM_BASE_URL", "")),
            "default_model": str(getattr(settings, "LOCAL_LLM_MODEL", "")).strip(),
            "error": str(exc),
            "models": [],
        }

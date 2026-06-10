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


class LocalLLMBadRequest(LocalLLMUnavailable):
    """Raised when the local LLM rejects the request payload (HTTP 400).

    Subclasses ``LocalLLMUnavailable`` so existing ``except`` blocks keep
    working; the agent loop catches this specifically to fall back to the
    plain no-tools chat path when a loaded model rejects ``tools``.
    """


@dataclass(frozen=True)
class ChatResult:
    content: str
    model: str
    usage: dict[str, Any]
    raw: dict[str, Any]


@dataclass(frozen=True)
class ToolCallRequest:
    """One function call requested by the model."""

    id: str
    name: str
    arguments: dict[str, Any]
    raw_arguments: str


@dataclass(frozen=True)
class ToolChatResult:
    """Result of a tool-enabled chat turn.

    ``content`` may legitimately be empty when the model requested tool
    calls instead of answering. ``assistant_message`` is the verbatim
    assistant message dict so the caller can append it to the running
    conversation before adding ``role:"tool"`` results.
    """

    content: str
    tool_calls: tuple[ToolCallRequest, ...]
    model: str
    usage: dict[str, Any]
    raw: dict[str, Any]
    assistant_message: dict[str, Any]


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
            with urlopen(request, timeout=self.timeout_seconds) as response:  # noqa: S310  # nosec B310
                text = response.read().decode("utf-8")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            if exc.code == 400:
                raise LocalLLMBadRequest(f"Local LLM rejected the request: {detail}") from exc
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

    def chat_with_tools(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]],
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        tool_choice: str = "auto",
    ) -> ToolChatResult:
        """One tool-enabled chat turn against the OpenAI-compatible server.

        Unlike :meth:`chat`, an empty ``content`` is valid here when the
        model returned ``tool_calls`` instead of a final answer. Messages
        are passed through verbatim, so callers may include prior
        assistant messages carrying ``tool_calls`` and ``role:"tool"``
        results keyed by ``tool_call_id``.
        """
        resolved_model = self.resolve_model(model)
        payload: dict[str, Any] = {
            "model": resolved_model,
            "messages": list(messages),
            "temperature": temperature,
            "max_tokens": int(max_tokens or getattr(settings, "LOCAL_LLM_MAX_TOKENS", 900)),
            "tools": tools,
            "tool_choice": tool_choice,
        }
        data = self._request("POST", "/chat/completions", payload)
        choices = data.get("choices") or []
        first = choices[0] if choices and isinstance(choices[0], dict) else {}
        message = first.get("message") if isinstance(first.get("message"), dict) else {}
        content = str(message.get("content") or "").strip()

        tool_calls: list[ToolCallRequest] = []
        for entry in message.get("tool_calls") or []:
            if not isinstance(entry, dict):
                continue
            function = entry.get("function") if isinstance(entry.get("function"), dict) else {}
            name = str(function.get("name") or "").strip()
            if not name:
                continue
            raw_arguments = str(function.get("arguments") or "")
            try:
                parsed = json.loads(raw_arguments) if raw_arguments.strip() else {}
            except json.JSONDecodeError:
                parsed = {}
            tool_calls.append(
                ToolCallRequest(
                    id=str(entry.get("id") or f"call_{len(tool_calls)}"),
                    name=name,
                    arguments=parsed if isinstance(parsed, dict) else {},
                    raw_arguments=raw_arguments,
                )
            )

        if not content and not tool_calls:
            reasoning = str(message.get("reasoning_content", "")).strip()
            finish_reason = str(first.get("finish_reason", "")).strip()
            if reasoning and finish_reason == "length":
                raise LocalLLMUnavailable(
                    "Local LLM used the full token budget for hidden reasoning before a final "
                    "answer. Increase LOCAL_LLM_MAX_TOKENS or choose a non-thinking model."
                )
            raise LocalLLMUnavailable(
                "Local LLM returned neither content nor tool calls for a tool-enabled turn."
            )

        # Verbatim assistant message for conversation continuity; LM Studio
        # expects the original tool_calls block to precede role:"tool" results.
        assistant_message: dict[str, Any] = {"role": "assistant", "content": content or ""}
        if message.get("tool_calls"):
            assistant_message["tool_calls"] = message["tool_calls"]

        return ToolChatResult(
            content=content,
            tool_calls=tuple(tool_calls),
            model=str(data.get("model") or resolved_model),
            usage=data.get("usage") if isinstance(data.get("usage"), dict) else {},
            raw=data,
            assistant_message=assistant_message,
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

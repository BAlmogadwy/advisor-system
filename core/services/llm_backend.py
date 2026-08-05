"""One OpenAI-compatible client, two configured backends.

The adviser has always spoken to an OpenAI-compatible `/chat/completions`
endpoint. Which endpoint that is — a local LM Studio server, or Alibaba Cloud
Model Studio — is a matter of CONFIGURATION, not of code. Returning to local is
`LLM_BACKEND=local` and a restart: no rollback, no prompt fork, no tool-registry
fork, no branch change.

WHAT IS DELIBERATELY NOT HERE

  * **No provider fallback, in either direction.** An Alibaba failure never
    silently reaches for the local server, and a local failure never reaches for
    a paid API. A backend that cannot answer fails as itself. The adviser's
    existing same-provider recovery paths — tools rejected, so answer without
    them — are untouched, because they never leave the configured backend.
  * **No model discovery on a remote backend.** The local client may ask the
    server what is loaded, because that is a machine the operator owns. A remote
    model is named explicitly or the configuration is refused; discovering one
    means paying for whichever model a workspace happens to list first.
  * **No `raw` on the results.** It carried the entire provider response —
    including `reasoning_content` — into every caller, and nothing read it.
    Removed rather than sanitised: a field with no consumer is a leak with no
    benefit.

ERROR SHAPE

The neutral names are the real classes; the `LocalLLM*` names are ALIASES, not
subclasses. That distinction matters: the agent loop catches
`except LocalLLMBadRequest`, and a compatibility subclass whose parent is what
actually gets raised would silently stop catching.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from django.conf import settings

logger = logging.getLogger(__name__)


# ── errors ───────────────────────────────────────────────────────


class LLMError(RuntimeError):
    """Base error for LLM integration failures."""


class LLMConfigError(LLMError):
    """The configured endpoint is unsafe, incomplete or invalid."""


class LLMAuthenticationError(LLMConfigError):
    """The provider rejected the credentials (401/403).

    A CONFIG error, not an availability one, and that is a behavioural choice:
    the adviser's degradation paths catch `LLMUnavailable` so a flaky endpoint
    still produces an answer. A rejected key will be rejected again next second,
    so degrading merely hides it. It propagates.
    """


class LLMUnavailable(LLMError):
    """The endpoint could not be reached, or could not produce a usable turn."""


class LLMBadRequest(LLMUnavailable):
    """The provider rejected the request payload (HTTP 400).

    Subclasses `LLMUnavailable` so existing `except` blocks keep working; the
    agent loop catches this specifically to fall back to the plain no-tools chat
    path when a model rejects `tools`.
    """


class LLMRateLimited(LLMUnavailable):
    """429. Retried within bounds, honouring `Retry-After` when it is sane."""


class LLMTimeout(LLMUnavailable):
    """The request exceeded its timeout after the permitted retries."""


class LLMInvalidResponse(LLMUnavailable):
    """A 200 that carries no usable turn — no content and no tool calls."""


class LLMPrivacyError(LLMError):
    """A remote request was about to carry data with no approved projection.

    Raised BEFORE serialisation. Fails closed by construction: an unknown tool
    result has no allowlist, so it cannot be sent by accident merely because
    nobody thought to blacklist its keys.
    """


#: Aliases, not subclasses — see the module docstring.
LocalLLMError = LLMError
LocalLLMConfigError = LLMConfigError
LocalLLMUnavailable = LLMUnavailable
LocalLLMBadRequest = LLMBadRequest


# ── results ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class ChatResult:
    content: str
    model: str
    usage: dict[str, Any]


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

    `content` may legitimately be empty when the model requested tool calls
    instead of answering. `assistant_message` is the verbatim assistant message
    dict so the caller can append it to the running conversation before adding
    `role:"tool"` results.
    """

    content: str
    tool_calls: tuple[ToolCallRequest, ...]
    model: str
    usage: dict[str, Any]
    assistant_message: dict[str, Any]


@dataclass
class UsageTotals:
    """Tokens across EVERY provider call in one adviser answer.

    The loop used to overwrite `usage` with the latest turn, so a question that
    took three tool-selection calls and a final answer reported the cost of the
    final call alone. That is not a cost comparison, it is the last quarter of
    one.
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    calls: int = 0

    def add(self, usage: dict[str, Any] | None) -> None:
        if not isinstance(usage, dict):
            return
        self.calls += 1
        prompt = int(usage.get("prompt_tokens") or 0)
        completion = int(usage.get("completion_tokens") or 0)
        self.prompt_tokens += prompt
        self.completion_tokens += completion
        # Providers do not all send `total_tokens`; derive it rather than
        # reporting a zero that looks like a measurement.
        self.total_tokens += int(usage.get("total_tokens") or (prompt + completion))

    def as_dict(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "provider_calls": self.calls,
        }


# ── configuration ────────────────────────────────────────────────

BACKEND_LOCAL = "local"
BACKEND_ALIBABA = "alibaba"
ALLOWED_BACKENDS = (BACKEND_LOCAL, BACKEND_ALIBABA)

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

#: Alibaba Model Studio / DashScope only. An arbitrary remote host would turn a
#: configuration mistake into an exfiltration channel, so the host is checked
#: rather than merely the scheme.
_ALIBABA_HOST_SUFFIXES = (
    ".aliyuncs.com",
    ".aliyun.com",
    ".alibabacloud.com",
)


@dataclass(frozen=True)
class LLMEndpointConfig:
    """Everything the client needs, and nothing about which product it serves.

    THE KEY IS EXCLUDED FROM THE REPR, and that is not decoration. A frozen
    dataclass prints every field by default, so `repr(config)` inside a pytest
    assertion diff, a `logger.debug("%s", config)`, or a traceback frame that
    happens to hold the config would print the bearer token in full. Nothing has
    to be careless for that to happen — the default behaviour is the leak.
    """

    backend: str
    provider: str
    base_url: str
    model: str
    timeout_seconds: float
    max_tokens: int
    max_retries: int
    api_key: str = field(default="", repr=False)
    enable_thinking: bool = False
    #: Local Qwen builds are fed a `<think></think>` prefill to suppress hidden
    #: reasoning. That is a property of the SERVER, not of the model name — a
    #: remote model called `qwen3.7-max` would match the name heuristic and be
    #: sent a prefill no compatibility test has approved.
    supports_assistant_prefill: bool = True
    #: Remote backends never discover a model; see the module docstring.
    allow_model_discovery: bool = True
    #: Remote backends get the explicit privacy projection.
    is_remote: bool = False
    #: Sent as top-level fields on remote requests.
    provider_options: dict[str, Any] = field(default_factory=dict)

    @property
    def endpoint_host(self) -> str:
        """The hostname alone — never the full URL, which may carry a path or
        query a browser has no business seeing."""
        return (urlparse(self.base_url).hostname or "").lower()


def _flag(name: str, default: bool) -> bool:
    value = getattr(settings, name, default)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def configured_backend() -> str:
    backend = str(getattr(settings, "LLM_BACKEND", BACKEND_LOCAL) or BACKEND_LOCAL).strip().lower()
    if backend not in ALLOWED_BACKENDS:
        raise LLMConfigError(
            f"LLM_BACKEND must be one of {', '.join(ALLOWED_BACKENDS)}; got {backend!r}."
        )
    return backend


def _local_config() -> LLMEndpointConfig:
    base_url = str(getattr(settings, "LOCAL_LLM_BASE_URL", "http://localhost:1234/v1")).strip()
    if not base_url:
        raise LLMConfigError("LOCAL_LLM_BASE_URL is empty.")

    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise LLMConfigError("LOCAL_LLM_BASE_URL must be an absolute http(s) URL.")

    host = (parsed.hostname or "").lower()
    if host not in _LOCAL_HOSTS and not _flag("LOCAL_LLM_ALLOW_REMOTE", False):
        raise LLMConfigError("Local advisor only permits localhost LLM endpoints.")

    return LLMEndpointConfig(
        backend=BACKEND_LOCAL,
        provider="local",
        base_url=base_url.rstrip("/"),
        model=str(getattr(settings, "LOCAL_LLM_MODEL", "")).strip(),
        timeout_seconds=float(getattr(settings, "LOCAL_LLM_TIMEOUT_SECONDS", 120)),
        max_tokens=int(getattr(settings, "LOCAL_LLM_MAX_TOKENS", 1400)),
        # Unchanged from before this module existed: the local path retried
        # nothing, and making it retry would be a behaviour change smuggled in
        # under a provider addition.
        max_retries=0,
        supports_assistant_prefill=True,
        allow_model_discovery=True,
        is_remote=False,
    )


def _alibaba_config() -> LLMEndpointConfig:
    base_url = str(getattr(settings, "ALIBABA_LLM_BASE_URL", "")).strip()
    api_key = str(getattr(settings, "ALIBABA_LLM_API_KEY", "")).strip()
    model = str(getattr(settings, "ALIBABA_LLM_MODEL", "")).strip()

    if not base_url:
        raise LLMConfigError("ALIBABA_LLM_BASE_URL is required when LLM_BACKEND=alibaba.")
    if not api_key:
        raise LLMConfigError("ALIBABA_LLM_API_KEY is required when LLM_BACKEND=alibaba.")
    if not model:
        # Never discovered, never defaulted: a silent choice of model is a silent
        # choice of price and of capability.
        raise LLMConfigError(
            "ALIBABA_LLM_MODEL is required when LLM_BACKEND=alibaba; the model is "
            "never discovered from the workspace."
        )

    parsed = urlparse(base_url)
    if parsed.scheme != "https":
        raise LLMConfigError("ALIBABA_LLM_BASE_URL must be an absolute https URL.")
    host = (parsed.hostname or "").lower()
    if not any(
        host == suffix.lstrip(".") or host.endswith(suffix) for suffix in _ALIBABA_HOST_SUFFIXES
    ):
        raise LLMConfigError(
            "ALIBABA_LLM_BASE_URL must be an Alibaba Model Studio/DashScope host; "
            f"{host!r} is not one."
        )

    enable_thinking = _flag("ALIBABA_LLM_ENABLE_THINKING", False)
    return LLMEndpointConfig(
        backend=BACKEND_ALIBABA,
        provider="alibaba-model-studio",
        base_url=base_url.rstrip("/"),
        model=model,
        timeout_seconds=float(getattr(settings, "ALIBABA_LLM_TIMEOUT_SECONDS", 75)),
        max_tokens=int(getattr(settings, "ALIBABA_LLM_MAX_TOKENS", 3000)),
        max_retries=int(getattr(settings, "ALIBABA_LLM_MAX_RETRIES", 2)),
        api_key=api_key,
        enable_thinking=enable_thinking,
        # No official compatibility result says prefill survives tool calling
        # here, so it is off until one does.
        supports_assistant_prefill=False,
        allow_model_discovery=False,
        is_remote=True,
        provider_options={
            # Non-thinking for the first comparison: the local model has already
            # spent whole tool turns on hidden reasoning, and a provider
            # comparison with two variables measures neither.
            "enable_thinking": enable_thinking,
            # Public web search is outside this adviser's evidence and citation
            # contract. Never enabled.
            "enable_search": False,
            # One tool at a time keeps the loop's dedup and budget accounting
            # meaningful.
            "parallel_tool_calls": False,
        },
    )


def endpoint_config(backend: str | None = None) -> LLMEndpointConfig:
    resolved = (backend or configured_backend()).strip().lower()
    if resolved == BACKEND_LOCAL:
        return _local_config()
    if resolved == BACKEND_ALIBABA:
        return _alibaba_config()
    raise LLMConfigError(
        f"LLM_BACKEND must be one of {', '.join(ALLOWED_BACKENDS)}; got {resolved!r}."
    )


# ── the client ───────────────────────────────────────────────────

#: Bounded, and short. Attempt immediately, then ~0.5s, then ~1.5s.
_RETRY_BACKOFF_SECONDS = (0.5, 1.5)

#: A provider may send anything in `Retry-After`. Honour it only when it is a
#: small sane number of seconds; otherwise a hostile or broken value parks a
#: student's question for an hour.
_MAX_HONOURED_RETRY_AFTER = 10.0


class OpenAICompatibleLLMClient:
    """One client, configured for whichever OpenAI-compatible endpoint is in use."""

    def __init__(
        self,
        config: LLMEndpointConfig | None = None,
        *,
        base_url: str | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        if config is None:
            config = endpoint_config()
            if base_url or timeout_seconds is not None:
                # Kept for the existing `LocalLLMClient(base_url=...)` callers and
                # their tests.
                config = LLMEndpointConfig(
                    **{
                        **config.__dict__,
                        "base_url": (base_url or config.base_url).rstrip("/"),
                        "timeout_seconds": float(
                            timeout_seconds
                            if timeout_seconds is not None
                            else config.timeout_seconds
                        ),
                    }
                )
        self.config = config

    # Compatibility surface: callers and tests read `.base_url`/`.timeout_seconds`.
    @property
    def base_url(self) -> str:
        return self.config.base_url

    @property
    def timeout_seconds(self) -> float:
        return self.config.timeout_seconds

    @property
    def backend(self) -> str:
        return self.config.backend

    @property
    def supports_assistant_prefill(self) -> bool:
        return self.config.supports_assistant_prefill

    # ── transport ────────────────────────────────────────────────
    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        return headers

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        """One HTTP round trip, with bounded retries for the retryable failures.

        SAFE TO RETRY: this is the LLM call itself, which is read-only. The
        caller retries nothing — a local capability that has already run is never
        re-executed because a later provider call failed.

        No response body ever reaches an exception message. The old client put
        the provider's reply into the error string, which is how a student
        record echoed back by a provider would land in a log line.
        """
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        effective_timeout = timeout_seconds if timeout_seconds else self.config.timeout_seconds
        attempts = self.config.max_retries + 1
        last: LLMError | None = None

        for attempt in range(attempts):
            request = Request(
                f"{self.config.base_url}{path}",
                data=body,
                method=method,
                headers=self._headers(),
            )
            try:
                with urlopen(request, timeout=effective_timeout) as response:  # noqa: S310  # nosec B310
                    text = response.read().decode("utf-8")
            except HTTPError as exc:
                last, retryable, wait = self._classify_http(exc)
            except URLError as exc:
                reason = type(getattr(exc, "reason", exc)).__name__
                last, retryable, wait = (
                    LLMUnavailable(f"{self.config.provider} is not reachable ({reason})."),
                    self.config.max_retries > 0,
                    None,
                )
            except TimeoutError:
                last, retryable, wait = (
                    LLMTimeout(f"{self.config.provider} request timed out."),
                    self.config.max_retries > 0,
                    None,
                )
            else:
                try:
                    return json.loads(text)
                except json.JSONDecodeError as exc:
                    raise LLMInvalidResponse(
                        f"{self.config.provider} returned invalid JSON."
                    ) from exc

            if not retryable or attempt == attempts - 1:
                raise last
            delay = (
                wait
                if wait is not None
                else _RETRY_BACKOFF_SECONDS[min(attempt, len(_RETRY_BACKOFF_SECONDS) - 1)]
            )
            logger.warning(
                "llm retry backend=%s host=%s attempt=%d/%d category=%s",
                self.config.backend,
                self.config.endpoint_host,
                attempt + 1,
                attempts,
                type(last).__name__,
            )
            time.sleep(delay)

        raise last or LLMUnavailable(f"{self.config.provider} failed.")

    def _classify_http(self, exc: HTTPError) -> tuple[LLMError, bool, float | None]:
        """Status → typed error, retryability, and any honoured wait.

        The provider's response body is READ AND DISCARDED. Only a status code
        and a parsed error code survive, because the body can contain the
        request echoed back.
        """
        status = int(getattr(exc, "code", 0) or 0)
        code = self._provider_error_code(exc)
        suffix = f" (provider code {code})" if code else ""

        if status == 400:
            return (
                LLMBadRequest(f"{self.config.provider} rejected the request{suffix}."),
                False,
                None,
            )
        if status in (401, 403):
            return (
                LLMAuthenticationError(
                    f"{self.config.provider} rejected the credentials (HTTP {status}){suffix}."
                ),
                False,
                None,
            )
        if status == 429:
            return (
                LLMRateLimited(f"{self.config.provider} rate limited the request{suffix}."),
                self.config.max_retries > 0,
                self._retry_after(exc),
            )
        if 500 <= status <= 599:
            return (
                LLMUnavailable(f"{self.config.provider} returned HTTP {status}{suffix}."),
                self.config.max_retries > 0,
                None,
            )
        return (
            LLMUnavailable(f"{self.config.provider} returned HTTP {status}{suffix}."),
            False,
            None,
        )

    @staticmethod
    def _provider_error_code(exc: HTTPError) -> str:
        """A short machine code from the error body, or nothing.

        Deliberately narrow: one short token, never a message, never a payload.
        """
        try:
            body = exc.read().decode("utf-8", errors="replace")
            parsed = json.loads(body)
        except Exception:  # noqa: BLE001 - a malformed error body is not an error
            return ""
        error = parsed.get("error") if isinstance(parsed, dict) else None
        code = ""
        if isinstance(error, dict):
            code = str(error.get("code") or error.get("type") or "")
        elif isinstance(parsed, dict):
            code = str(parsed.get("code") or "")
        code = code.strip()
        return (
            code[:40] if code.replace("_", "").replace(".", "").replace("-", "").isalnum() else ""
        )

    @staticmethod
    def _retry_after(exc: HTTPError) -> float | None:
        raw = ""
        try:
            raw = str(exc.headers.get("Retry-After") or "").strip()
        except Exception:  # noqa: BLE001
            return None
        try:
            seconds = float(raw)
        except ValueError:
            return None
        if 0 < seconds <= _MAX_HONOURED_RETRY_AFTER:
            return seconds
        return None

    # ── model ────────────────────────────────────────────────────
    def list_models(self) -> list[dict[str, Any]]:
        if not self.config.allow_model_discovery:
            raise LLMConfigError(
                f"{self.config.provider} does not permit model discovery; set an explicit model."
            )
        data = self._request("GET", "/models")
        return [m for m in data.get("data", []) if isinstance(m, dict)]

    def resolve_model(self, requested_model: str | None = None) -> str:
        candidate = (requested_model or self.config.model).strip()
        if candidate:
            return candidate
        if not self.config.allow_model_discovery:
            raise LLMConfigError(
                f"{self.config.provider} requires an explicit model; none is configured."
            )
        for item in self.list_models():
            model_id = str(item.get("id", "")).strip()
            if model_id:
                return model_id
        raise LLMUnavailable("No model is loaded in the local LLM server.")

    # ── payload ──────────────────────────────────────────────────
    def _base_payload(
        self, resolved_model: str, max_tokens: int | None, temperature: float
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": resolved_model,
            "temperature": temperature,
            "max_tokens": int(max_tokens or self.config.max_tokens),
        }
        # Provider options are top-level JSON fields on the raw request, which is
        # how Model Studio's OpenAI-compatible endpoint takes them.
        payload.update(self.config.provider_options)
        return payload

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
            if not self.config.supports_assistant_prefill:
                # Not silently dropped. A caller asking for prefill is asking for
                # a specific behaviour; on a backend that has never been shown to
                # preserve it, the honest answer is to say so.
                raise LLMConfigError(
                    f"{self.config.provider} has no verified assistant-prefill support; "
                    "call without assistant_prefill on this backend."
                )
            request_messages.append({"role": "assistant", "content": assistant_prefill})

        payload = self._base_payload(resolved_model, max_tokens, temperature)
        payload["messages"] = request_messages
        data = self._request("POST", "/chat/completions", payload)

        choices = data.get("choices") or []
        first = choices[0] if choices and isinstance(choices[0], dict) else {}
        message = first.get("message") if isinstance(first.get("message"), dict) else {}
        content = str(message.get("content", "")).strip()
        if not content:
            self._raise_for_empty(message, first, tool_enabled=False)

        return ChatResult(
            content=content,
            model=str(data.get("model") or resolved_model),
            usage=data.get("usage") if isinstance(data.get("usage"), dict) else {},
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
        timeout_seconds: float | None = None,
    ) -> ToolChatResult:
        """One tool-enabled chat turn.

        Unlike `chat`, an empty `content` is valid here when the model returned
        `tool_calls` instead of a final answer. Messages pass through verbatim,
        so callers may include prior assistant messages carrying `tool_calls` and
        `role:"tool"` results keyed by `tool_call_id`.
        """
        resolved_model = self.resolve_model(model)
        payload = self._base_payload(resolved_model, max_tokens, temperature)
        payload["messages"] = list(messages)
        payload["tools"] = tools
        payload["tool_choice"] = tool_choice
        data = self._request("POST", "/chat/completions", payload, timeout_seconds=timeout_seconds)

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
            self._raise_for_empty(message, first, tool_enabled=True)

        assistant_message: dict[str, Any] = {"role": "assistant", "content": content or ""}
        if message.get("tool_calls"):
            assistant_message["tool_calls"] = message["tool_calls"]

        return ToolChatResult(
            content=content,
            tool_calls=tuple(tool_calls),
            model=str(data.get("model") or resolved_model),
            usage=data.get("usage") if isinstance(data.get("usage"), dict) else {},
            assistant_message=assistant_message,
        )

    def _raise_for_empty(
        self, message: dict[str, Any], choice: dict[str, Any], *, tool_enabled: bool
    ) -> None:
        """A 200 with nothing usable in it.

        `reasoning_content` is INSPECTED to classify the failure and never
        quoted: it is the model's hidden thinking, it is not for students, and it
        is not for logs.
        """
        has_reasoning = bool(str(message.get("reasoning_content", "")).strip())
        finish_reason = str(choice.get("finish_reason", "")).strip()
        if has_reasoning and finish_reason == "length":
            raise LLMInvalidResponse(
                f"{self.config.provider} used the full token budget for hidden reasoning "
                "before a final answer. Raise the token limit or disable thinking mode."
            )
        if has_reasoning and not tool_enabled:
            raise LLMInvalidResponse(
                f"{self.config.provider} returned hidden reasoning but no final answer. "
                "Choose a model that emits message.content for advisor responses."
            )
        if tool_enabled:
            raise LLMInvalidResponse(
                f"{self.config.provider} returned neither content nor tool calls "
                "for a tool-enabled turn."
            )
        raise LLMInvalidResponse(f"{self.config.provider} returned an empty response.")


class LocalLLMClient(OpenAICompatibleLLMClient):
    """The pre-existing name, pinned to the LOCAL backend.

    Kept so that `LocalLLMClient(base_url="http://localhost:1234/v1")` in the
    existing tests, and the two evaluation scripts, keep meaning what they meant.
    It never follows `LLM_BACKEND` — a class called *Local* that quietly spoke to
    a paid API would be the worst possible surprise.
    """

    def __init__(self, base_url: str | None = None, timeout_seconds: float | None = None) -> None:
        config = _local_config()
        if base_url or timeout_seconds is not None:
            config = LLMEndpointConfig(
                **{
                    **config.__dict__,
                    "base_url": (base_url or config.base_url).rstrip("/"),
                    "timeout_seconds": float(
                        timeout_seconds if timeout_seconds is not None else config.timeout_seconds
                    ),
                }
            )
        super().__init__(config)


def get_llm_client(backend: str | None = None) -> OpenAICompatibleLLMClient:
    """THE factory. One call site decides the backend; nothing else branches."""
    return OpenAICompatibleLLMClient(endpoint_config(backend))


# ── health ───────────────────────────────────────────────────────


def check_llm_health(backend: str | None = None) -> dict[str, Any]:
    """Configuration-only. Never makes a paid inference call.

    What it reports is deliberately narrow: enough to diagnose a misconfiguration,
    never enough to leak one. No API key, no full URL — the hostname alone, because
    a Model Studio URL can carry a workspace path.
    """
    try:
        config = endpoint_config(backend)
    except LLMConfigError as exc:
        return {
            "ok": False,
            "configured": False,
            "backend": str(getattr(settings, "LLM_BACKEND", BACKEND_LOCAL)).strip().lower(),
            "provider": "",
            "model": "",
            "endpoint_host": "",
            "error_category": type(exc).__name__,
            "error": str(exc),
            "models": [],
        }

    payload: dict[str, Any] = {
        "ok": True,
        "configured": True,
        "backend": config.backend,
        "provider": config.provider,
        "model": config.model,
        "endpoint_host": config.endpoint_host,
        "models": [],
    }

    if config.backend == BACKEND_LOCAL:
        # A local model list is free and is the only useful local diagnostic.
        try:
            client = OpenAICompatibleLLMClient(config)
            payload["models"] = [
                {"id": str(m.get("id", "")).strip(), "object": str(m.get("object", ""))}
                for m in client.list_models()
                if str(m.get("id", "")).strip()
            ]
        except LLMError as exc:
            payload.update({"ok": False, "error_category": type(exc).__name__, "error": str(exc)})
    return payload


def check_local_llm_health() -> dict[str, Any]:
    """Compatibility wrapper. Reports whichever backend is configured."""
    health = check_llm_health()
    # The old shape carried `base_url` and `default_model`; keep them meaningful
    # without widening what a browser can see.
    health.setdefault("default_model", health.get("model", ""))
    health["base_url"] = health.get("endpoint_host", "")
    return health


__all__ = [
    "ALLOWED_BACKENDS",
    "BACKEND_ALIBABA",
    "BACKEND_LOCAL",
    "ChatResult",
    "LLMAuthenticationError",
    "LLMBadRequest",
    "LLMConfigError",
    "LLMEndpointConfig",
    "LLMError",
    "LLMInvalidResponse",
    "LLMPrivacyError",
    "LLMRateLimited",
    "LLMTimeout",
    "LLMUnavailable",
    "LocalLLMBadRequest",
    "LocalLLMClient",
    "LocalLLMConfigError",
    "LocalLLMError",
    "LocalLLMUnavailable",
    "OpenAICompatibleLLMClient",
    "ToolCallRequest",
    "ToolChatResult",
    "UsageTotals",
    "check_llm_health",
    "check_local_llm_health",
    "configured_backend",
    "endpoint_config",
    "get_llm_client",
]

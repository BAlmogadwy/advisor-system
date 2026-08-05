"""Compatibility surface for the pre-provider-neutral client.

The implementation moved to `core.services.llm_backend`, which serves either a
local OpenAI-compatible server or Alibaba Cloud Model Studio from one code path.
Everything here is a re-export so existing imports keep working:

    from core.services.local_llm import LocalLLMClient, ChatResult, ...

The `LocalLLM*` exception names are ALIASES of the neutral classes, not
subclasses of them. That is load-bearing: the agent loop catches
`except LocalLLMBadRequest`, and a compatibility subclass whose neutral parent is
what actually gets raised would stop catching without any test noticing.

`LocalLLMClient` remains pinned to the LOCAL backend and never follows
`LLM_BACKEND` — a class named *Local* that quietly spoke to a paid API is the
worst surprise this refactor could ship. Production code obtains its client from
`llm_backend.get_llm_client()` instead.
"""

from core.services.llm_backend import (
    ChatResult,
    LocalLLMBadRequest,
    LocalLLMClient,
    LocalLLMConfigError,
    LocalLLMError,
    LocalLLMUnavailable,
    ToolCallRequest,
    ToolChatResult,
    check_local_llm_health,
)

__all__ = [
    "ChatResult",
    "LocalLLMBadRequest",
    "LocalLLMClient",
    "LocalLLMConfigError",
    "LocalLLMError",
    "LocalLLMUnavailable",
    "ToolCallRequest",
    "ToolChatResult",
    "check_local_llm_health",
]

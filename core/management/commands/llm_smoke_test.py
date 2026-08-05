"""A deliberate, synthetic, owner-approved live call to a configured LLM backend.

SEPARATE FROM THE HEALTH CHECK ON PURPOSE. `check_llm_health()` is
configuration-only and free, and it is what the application and the browser use.
This command spends money, so it is a thing an operator has to type.

    python manage.py llm_smoke_test --backend alibaba

WHAT IT SENDS

Synthetic data only. No student record, no real capability, no database read —
the tool in the round trip is a fixture that returns a hard-coded number. That is
not a limitation of this command, it is the point: proving the transport,
authentication, tool-calling and Arabic handling of a provider requires none of
a student's data, so none is sent.

WHAT IT PRINTS

Provider, model, latency, token usage, and whether a tool was correctly selected.
Never the request payload, never `reasoning_content`, never the API key.
"""

from __future__ import annotations

import json
import time
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from core.services.llm_backend import (
    ALLOWED_BACKENDS,
    LLMConfigError,
    LLMError,
    endpoint_config,
    get_llm_client,
)

#: A tool with no database behind it. `credits` is a fixture value; the point is
#: whether the provider selects the tool and returns a well-formed call, not what
#: the answer is.
SYNTHETIC_TOOL = {
    "type": "function",
    "function": {
        "name": "lookup_demo_course_credits",
        "description": "Return the credit hours of a demo course code. Test fixture only.",
        "parameters": {
            "type": "object",
            "properties": {
                "course_code": {"type": "string", "description": "e.g. DEMO101"},
            },
            "required": ["course_code"],
        },
    },
}

ARABIC_PROMPT = "ما معنى الساعة المعتمدة في الجامعة؟ أجب بجملتين فقط."
TOOL_PROMPT = "كم عدد الساعات المعتمدة لمقرر DEMO101؟ استخدم الأداة المتاحة."


class Command(BaseCommand):
    help = "Make a small, synthetic, PAID live request to a configured LLM backend."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--backend", choices=list(ALLOWED_BACKENDS), required=True)
        parser.add_argument(
            "--max-tokens",
            type=int,
            default=200,
            help="Kept small: this is a transport check, not a generation test.",
        )
        parser.add_argument(
            "--yes",
            action="store_true",
            help="Required for a remote backend. States that a paid request is intended.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        backend = options["backend"]
        try:
            config = endpoint_config(backend)
        except LLMConfigError as exc:
            raise CommandError(f"{backend} is not configured: {exc}") from exc

        if config.is_remote and not options["yes"]:
            raise CommandError(
                f"This will make PAID requests to {config.provider} "
                f"(region {config.region}) using model {config.model}. "
                "Re-run with --yes to confirm."
            )

        self.stdout.write(f"backend        : {config.backend}")
        self.stdout.write(f"provider       : {config.provider}")
        self.stdout.write(f"model          : {config.model}")
        # REGION, never the host: the first hostname label is the workspace id.
        self.stdout.write(f"region         : {config.region}")
        self.stdout.write(f"thinking       : {config.enable_thinking}")
        self.stdout.write(f"prefill support: {config.supports_assistant_prefill}")
        if config.is_remote:
            self.stdout.write(
                self.style.WARNING("\nSYNTHETIC DATA ONLY — no student record is sent.")
            )

        client = get_llm_client(backend)
        max_tokens = int(options["max_tokens"])

        # ── 1. a plain Arabic answer ─────────────────────────────
        self.stdout.write("\n[1/2] plain Arabic answer")
        started = time.perf_counter()
        try:
            result = client.chat(
                [
                    {"role": "system", "content": "You are a concise university adviser."},
                    {"role": "user", "content": ARABIC_PROMPT},
                ],
                max_tokens=max_tokens,
            )
        except LLMError as exc:
            raise CommandError(f"{type(exc).__name__}: {exc}") from exc
        latency = round((time.perf_counter() - started) * 1000)
        self._report(result.model, latency, result.usage)
        self.stdout.write(f"      answer   : {result.content[:160]}")
        arabic = any("؀" <= ch <= "ۿ" for ch in result.content)
        self.stdout.write(f"      arabic   : {arabic}")

        # ── 2. one tool-call round trip ──────────────────────────
        self.stdout.write("\n[2/2] tool-call round trip (synthetic tool)")
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": "Use the provided tool when it applies."},
            {"role": "user", "content": TOOL_PROMPT},
        ]
        started = time.perf_counter()
        try:
            turn = client.chat_with_tools(messages, tools=[SYNTHETIC_TOOL], max_tokens=max_tokens)
        except LLMError as exc:
            raise CommandError(f"{type(exc).__name__}: {exc}") from exc
        latency = round((time.perf_counter() - started) * 1000)
        self._report(turn.model, latency, turn.usage)

        names = [call.name for call in turn.tool_calls]
        self.stdout.write(f"      requested: {names or '(none)'}")
        if not turn.tool_calls:
            self.stdout.write(
                self.style.WARNING("      the model answered without calling the tool")
            )
            return

        # The continuation: assistant tool_calls, then role="tool" keyed by id.
        messages.append(turn.assistant_message)
        for call in turn.tool_calls:
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "name": call.name,
                    "content": json.dumps({"course_code": "DEMO101", "credits": 3}),
                }
            )
        started = time.perf_counter()
        try:
            final = client.chat_with_tools(messages, tools=[SYNTHETIC_TOOL], max_tokens=max_tokens)
        except LLMError as exc:
            raise CommandError(f"continuation failed — {type(exc).__name__}: {exc}") from exc
        latency = round((time.perf_counter() - started) * 1000)
        self._report(final.model, latency, final.usage)
        self.stdout.write(f"      final    : {final.content[:160]}")

        self.stdout.write(self.style.SUCCESS("\nSmoke test completed."))

    def _report(self, model: str, latency_ms: int, usage: dict[str, Any]) -> None:
        prompt = usage.get("prompt_tokens", "?")
        completion = usage.get("completion_tokens", "?")
        total = usage.get("total_tokens", "?")
        self.stdout.write(
            f"      model={model}  latency={latency_ms}ms  "
            f"prompt={prompt} completion={completion} total={total}"
        )

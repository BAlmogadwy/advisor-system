"""A deterministic stand-in for the provider. Transport only — nothing else is faked.

WHY IT MUST NOT READ THE SPEC

The obvious fake writes its answer from `must_assert`, and then the report says
spec → fake → scorer → spec. Every case passes and nothing has been measured. So
this one never sees the contract: it picks a tool from the surface the SERVER
advertised, and it renders its final answer from the tool RESULT the server produced.
The scorer reads the same contract independently, and the only thing joining them is
the system under test.

WHAT IT IS ALLOWED TO DECIDE

Exactly what a provider decides: which of the offered tools to call, with what
arguments, and what sentence to write. Everything else — routing, composition, the
policy contract, retrieval, narrowing, execution, provenance, postconditions — is the
real code, because those are the layers the evaluation exists to measure.

THE RENDERER IS DELIBERATELY DULL

It states what the payload holds and nothing more. A cleverer renderer would start
passing `factual_grounding` for reasons the real model would not share, which makes
the mock a worse predictor of the live run rather than a better one.
"""

from __future__ import annotations

import json
from typing import Any

from core.services.llm_backend import ChatResult, ToolCallRequest, ToolChatResult


def _course_codes(question: str) -> list[str]:
    import re

    return [m.group(0).upper() for m in re.finditer(r"\b[A-Za-z]{2,4}-?\d{3}\b", question)]


#: Arguments a tool needs beyond the session's own scope. Kept minimal on purpose:
#: a fake that fills every optional parameter tests the schema, not the product.
def _arguments_for(tool: str, question: str) -> dict[str, Any]:
    codes = _course_codes(question)
    if tool in {"why_course_locked", "lookup_course", "course_prerequisites"}:
        return {"course_code": codes[0]} if codes else {}
    if tool == "my_clash_free_sections":
        return {"course_codes": codes} if codes else {}
    if tool == "build_my_timetable":
        # `must_include` only when the student actually named a course. Filling it
        # from the recommender would be the fake doing the product's job.
        return {"must_include": codes} if codes else {}
    return {}


def _render(results: list[dict[str, Any]], question: str) -> str:
    """A plain statement of what the tools returned. No claims beyond them."""
    if not results:
        return "لا تتوفر لدي بيانات كافية للإجابة على هذا السؤال."
    lines: list[str] = []
    for row in results:
        tool = str(row.get("tool") or "")
        if row.get("ok") is False:
            lines.append(f"تعذر تنفيذ {tool}.")
            continue
        if tool == "build_my_timetable":
            kept = [r.get("course_code") for r in row.get("retained_sections") or []]
            added = [
                f"{r.get('course_code')} شعبة {r.get('section')}"
                for r in row.get("new_sections") or []
            ]
            unplaced = [r.get("course_code") for r in row.get("unplaced_courses") or []]
            summary = row.get("credit_summary") or {}
            if kept:
                lines.append("الشعب المسجلة حاليًا والمحتفظ بها: " + "، ".join(map(str, kept)) + ".")
            if added:
                lines.append("المقررات المضافة: " + "، ".join(added) + ".")
            if unplaced:
                lines.append(
                    "لم يتم وضع: " + "، ".join(map(str, unplaced)) + " حسب البيانات المتوفرة."
                )
            if summary:
                lines.append(
                    f"الساعات المحتفظ بها {summary.get('retained_credit_hours')} "
                    f"والمضافة {summary.get('new_credit_hours')}."
                )
        elif tool == "why_course_locked":
            listed = row.get("listed_as_prerequisite_count")
            sole = row.get("sole_remaining_prerequisite_count")
            lines.append(f"حالة المقرر: {row.get('status')}.")
            if listed is not None:
                lines.append(f"يُذكر كمتطلب سابق لـ {listed} مقرر، وينتظره وحده {sole} مقرر.")
        elif tool == "my_progress":
            counts = row.get("counts") or {}
            lines.append(
                f"عدد المقررات المتاحة {counts.get('open')} والمقفلة {counts.get('locked')}."
            )
        elif tool == "my_timetable":
            lines.append(f"عدد المحاضرات المسجلة: {len(row.get('meetings') or [])}.")
        else:
            lines.append(f"نتيجة {tool} متوفرة.")
    lines.append("هذا اقتراح تخطيطي ولا يمثل تسجيلًا رسميًا.")
    return " ".join(lines)


class MockProvider:
    """Deterministic, offline, and counts its own HTTP-equivalent calls.

    `provider_calls` counts INFERENCE turns, not questions: a turn that requests a
    tool and then continues is two, and the live budget has to be spent in the same
    units or it means nothing.
    """

    supports_assistant_prefill = True

    def __init__(self, backend: str = "local", *, unsafe_final: str | None = None) -> None:
        self.backend = backend
        #: When set, the fake writes this instead of rendering the evidence. Used by
        #: the focused 7A tests to prove a bad draft is caught — never by the batch.
        self.unsafe_final = unsafe_final
        self.provider_calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.exposed_tools: list[str] = []
        self._called: set[str] = set()

    def resolve_model(self, model: str | None = None) -> str:
        return model or "mock-deterministic"

    def _account(self, messages: list[dict], text: str) -> None:
        self.provider_calls += 1
        # A crude but honest proxy: the live run reports the provider's own numbers,
        # and a mock that invented plausible ones would make the two runs look
        # comparable when they are not.
        self.prompt_tokens += sum(len(str(m.get("content") or "")) for m in messages) // 4
        self.completion_tokens += len(text) // 4

    def chat_with_tools(self, messages, *, tools=None, **kwargs) -> ToolChatResult:
        offered = [(t.get("function") or {}).get("name") for t in (tools or [])]
        self.exposed_tools = [name for name in offered if name]
        question = str((messages[-1] or {}).get("content") or "")

        # One tool at most, and only one that was actually offered — the whole point
        # of 7B is that this list is the model's entire choice.
        candidate = next((n for n in self.exposed_tools if n not in self._called), None)
        if candidate:
            self._called.add(candidate)
            self._account(messages, candidate)
            arguments = _arguments_for(candidate, question)
            call = ToolCallRequest(
                id=f"call_{len(self._called)}",
                name=candidate,
                arguments=arguments,
                raw_arguments=json.dumps(arguments, ensure_ascii=False),
            )
            return ToolChatResult(
                content="",
                tool_calls=(call,),
                model=self.resolve_model(),
                usage={},
                assistant_message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {"name": call.name, "arguments": call.raw_arguments},
                        }
                    ],
                },
            )

        results = []
        for message in messages:
            if message.get("role") == "tool":
                try:
                    results.append(json.loads(str(message.get("content") or "{}")))
                except json.JSONDecodeError:
                    pass
        final = self.unsafe_final or _render(results, question)
        self._account(messages, final)
        return ToolChatResult(
            content=final,
            tool_calls=(),
            model=self.resolve_model(),
            usage={},
            assistant_message={"role": "assistant", "content": final},
        )

    def chat(self, messages, **kwargs) -> ChatResult:
        question = str((messages[-1] or {}).get("content") or "")
        final = self.unsafe_final or _render([], question)
        self._account(messages, final)
        return ChatResult(content=final, model=self.resolve_model(), usage={})


__all__ = ["MockProvider"]

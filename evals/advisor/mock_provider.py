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


#: How many tools one answer may draw on. Two is what the widest case in the
#: contract asks for; the cap stops a broad surface turning into twelve calls.
_MAX_TOOLS = 3

#: What a sentence is ABOUT, in the crude way a model reading it would notice. Keyed
#: on words, so the mock can rank a broad registry without being told the answer.
#: Ordered most-specific first. A general word like «مقرر» appears in almost every
#: question, so a table that puts it early ranks `lookup_course` above the capability
#: the sentence is actually about — which is how a broad surface produced three
#: irrelevant calls on TT13.
_AFFINITY: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (
        (
            "كم فصل",
            "مدة إنهاء",
            "التخرج",
            "أتخرج",
            "graduat",
            "terms left",
        ),
        ("graduation_progress",),
    ),
    # "registered but has no lecture time" — a registered-timetable read, not a
    # prerequisite question, even though it says «مقرر».
    (("وقت محاضرة", "ما له وقت", "بدون وقت", "no meeting", "lecture time"), ("my_timetable",)),
    # "am I still recommended to take it" — the recommendation and the record.
    (
        ("توصي", "يوصى", "ضمن المقررات التي", "recommend"),
        ("recommend_courses", "get_student_context"),
    ),
    # "if I defer X, what falls behind" — the chain, then the ranking.
    (("اجلت", "أجلت", "تاخر", "تتأخر", "defer", "delay"), ("why_course_locked", "my_progress")),
    # "choose three by impact, within the load limit" — ranking plus the term's
    # recommendation.
    (
        ("اخترها", "مساحة", "بحد الساعات", "choose", "capacity"),
        ("my_progress", "recommend_courses"),
    ),
    (
        ("متطلب", "المتطلبات", "مقفل", "يفتح", "ينتظر", "prerequisite", "locked", "unlock"),
        ("why_course_locked", "course_prerequisites"),
    ),
    # «رتّب … حسب تأثير كل واحد» is a ranking question that never says «أولوية», and
    # it mentions «الخطة» — so without this it matched the plan rule and fetched the
    # degree plan instead of the impact ranking.
    (
        (
            "رتب",
            "رتّب",
            "تاثير",
            "تأثير",
            "اثرها",
            "أثرها",
            "اولوي",
            "الأولوية",
            "اهم",
            "ترتيب",
            "priority",
            "rank",
            "impact",
        ),
        ("my_progress", "why_course_locked"),
    ),
    (("تعارض", "شعب", "clash", "section"), ("my_clash_free_sections",)),
    (("جدولي", "المسجل", "registered", "my timetable"), ("my_timetable",)),
    (("ابن", "جدول", "بناء", "build", "timetable"), ("build_my_timetable",)),
    (("خطة", "الخطة", "plan", "level"), ("my_plan_by_term",)),
    (("يوصي", "توصي", "recommend"), ("recommend_courses",)),
    (("مقرر", "course"), ("lookup_course",)),
)


def _latest_question(messages: list[dict]) -> str:
    """The student's sentence, not the whole prompt.

    The user message carries `verified_context` as JSON before the question, and that
    JSON mentions recommendations, timetables and prerequisites for every student. A
    fake that ranks tools over the whole message is matching the CONTEXT, not the
    question — which is how «إذا أجلت AI331 فصلًا» came out as a recommendation
    lookup. A real model reads the question; so does this.
    """
    for message in reversed(messages or []):
        content = str(message.get("content") or "")
        for marker in ("latest_question:", "student_question:"):
            if marker not in content:
                continue
            question = content.split(marker, 1)[1]
            # V2 appends locally retrieved policy evidence after the question. Tool
            # affinity must be decided from the student's words, not from that JSON.
            question = question.split("\nverified_", 1)[0]
            return question.strip()
    return str((messages[-1] or {}).get("content") or "") if messages else ""


def _relevant_tools(question: str, exposed: list[str]) -> list[str]:
    """The offered tools this sentence plausibly needs, most relevant first.

    Falls back to the advertised ORDER when nothing matches, which is what a narrowed
    route already is: the server put the right tool first.
    """
    text = str(question or "")
    for words, tools in _AFFINITY:
        if any(w in text for w in words):
            ranked = [tool for tool in tools if tool in exposed]
            if ranked:
                # Rules are ordered most-specific first. Once one family matches,
                # broader words such as «متطلبات» must not append unrelated tools.
                return ranked
    return list(exposed)


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
            registrations = [
                registration
                for registration in (row.get("registrations") or [])
                if registration.get("course_code") and registration.get("section")
            ]
            # Remote projection intentionally uses meeting rows rather than the
            # richer local registrations collection. Render the exact visible rows
            # in either mode; never reach back to the local agent result.
            if not registrations:
                registrations = list(
                    {
                        (str(meeting["course_code"]), str(meeting["section"])): {
                            "course_code": meeting["course_code"],
                            "section": meeting["section"],
                        }
                        for meeting in (row.get("meetings") or [])
                        if isinstance(meeting, dict)
                        and meeting.get("course_code")
                        and meeting.get("section")
                    }.values()
                )
            if registrations:
                rendered = [
                    f"{registration['course_code']} شعبة {registration['section']}"
                    for registration in registrations
                ]
                lines.append("المقررات والشعب في الجدول: " + "، ".join(rendered) + ".")
            else:
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
        question = _latest_question(messages)

        # A real turn calls what the QUESTION needs and stops. Walking the whole
        # advertised list would call twelve tools on a GENERAL_AGENT question and
        # none of the right ones on a narrowed route, which measured the fake rather
        # than the product. So: rank the offered tools by what the sentence is about,
        # take at most `_MAX_TOOLS`, and answer.
        #
        # The affinity table is keyed on QUESTION WORDS, never on the contract — the
        # mock has no idea which tools a case requires, and finding out would make
        # the report a statement about itself.
        wanted = _relevant_tools(question, self.exposed_tools)
        candidate = (
            next((n for n in wanted if n not in self._called), None)
            if len(self._called) < _MAX_TOOLS
            else None
        )
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
        question = _latest_question(messages)
        final = self.unsafe_final or _render([], question)
        self._account(messages, final)
        return ChatResult(content=final, model=self.resolve_model(), usage={})


__all__ = ["MockProvider"]

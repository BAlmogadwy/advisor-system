"""The turns the adviser is allowed to remember.

The conversation was durable for the student to READ and amnesiac for the adviser
to ANSWER: every turn was persisted, rendered and owned correctly, and then the
next question was generated with no knowledge of any of it. So the interface
looked conversational while «احتفظ بها», «الثاني أفضل» and «لا، أقصد الفصل القادم»
could never work — the pronoun had nothing to refer to.

What goes in is only what the student already saw: their questions and the
answers. Not tool results, not judge findings, not retrieved policy candidates,
not prompts. Those are the operator record; putting them in the model's context
would both leak internals into the next answer and spend the token budget on
material the agent loop re-derives anyway by calling its tools again.

**Policy ids are stripped.** A stored answer reads «الدليل…، ص 24
[TU.WITHDRAWAL.MAXIMUM]», and the bracketed id is the machine half of the
citation contract. Fed back verbatim, the model sees an id it did not retrieve
*this* turn, copies it forward, and `validate_citations` correctly rejects it as
NOT_RETRIEVED_THIS_REQUEST — turning a good follow-up answer into a citation
refusal. The human-readable reference stays; the id goes.
"""

from __future__ import annotations

import re
from typing import Any

from core.models import AdvisorMessage

#: Kept a little under the runtime's own `_MAX_HISTORY_MESSAGES` window so the
#: boundary is decided here, where the reason for it is written down, rather than
#: by whichever end happens to truncate first.
MAX_HISTORY_MESSAGES = 8

#: Mirrors `_POLICY_ID_RE` in the runtime: at least three dot-separated segments,
#: as in TU.WITHDRAWAL.MAXIMUM. Anything looser eats real content — `[W]` is the
#: withdrawal grade, and `[GPA]` is a word.
_POLICY_MARKER = re.compile(r"\s*\[[A-Z][A-Z0-9]*(?:\.[A-Z0-9_]+){2,}\]")

#: A turn only belongs in the history if the student actually saw it resolve.
#: PENDING is still being generated; FAILED never produced an answer, and feeding
#: back a question that was never answered invites the model to believe it already
#: replied.
_SETTLED = frozenset(
    {
        AdvisorMessage.STATUS_COMPLETED,
        AdvisorMessage.STATUS_ABSTAINED,
        AdvisorMessage.STATUS_ESCALATED,
    }
)


def load_visible_history(
    conversation: Any,
    *,
    exclude_message_id: Any = None,
    max_messages: int = MAX_HISTORY_MESSAGES,
) -> list[dict[str, str]]:
    """The prior turns of THIS conversation, oldest first.

    Scoped to one conversation object, which the caller has already proved the
    student owns — so cross-conversation and cross-student bleed are impossible
    here rather than merely unlikely.

    `exclude_message_id` matters more than it looks: the student's question is
    written to the database BEFORE generation, and a resumed retry reuses that same
    row. Without excluding it the model would receive the current question twice —
    once as history and once as the question — and on a retry it would see it
    doubled again.
    """
    settled = (
        conversation.messages.filter(status__in=_SETTLED)
        .exclude(pk=exclude_message_id)
        # NOT `pk`: it is a random UUID, and two messages from one request share a
        # `created_at` on a coarse clock — that tiebreak reversed question and answer.
        .order_by("sequence", "created_at")
        .values_list("role", "content")
    )

    turns: list[dict[str, str]] = []
    for role, content in settled:
        text = str(content or "").strip()
        if not text:
            continue
        if role == AdvisorMessage.ROLE_ASSISTANT:
            text = _POLICY_MARKER.sub("", text).strip()
            if not text:
                continue
            turns.append({"role": "assistant", "content": text})
        elif role == AdvisorMessage.ROLE_STUDENT:
            turns.append({"role": "user", "content": text})

    # The most recent window, still in chronological order — a history that ends
    # mid-exchange reads as if the adviser ignored the last thing said.
    return turns[-max_messages:]

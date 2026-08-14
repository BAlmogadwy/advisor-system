"""Getting an adviser's answer onto a phone without changing what it says.

Three problems, and the third is the one that bites.

**Markup.** The answer is written by a model and contains course codes, policy
references and bracketed ids. Sent under any Telegram parse mode those characters
become syntax. The gateway sends plain text and sets no `parse_mode` at all, so
there is no escaping to get subtly wrong — `escape_markdown_v2` exists here for a
future caller that genuinely needs markup, and is tested, but nothing in the
delivery path uses it.

**Length.** Telegram refuses anything over 4096 characters, and truncation on a
course adviser is not a cosmetic failure: the part that falls off the end is the
part that says "confirm with the deanship before you rely on this".

**Which part falls off.** A naive splitter cuts at 4096 characters and hands the
student a message whose disclaimer landed in a chunk they may never scroll to. So
the split is boundary-aware — paragraph, then line, then word, never mid-word —
and the trailing block that carries the sources and the caveat is kept whole
rather than being wherever the arithmetic lands.

Nothing here adds information. No student number, no name, no derived figures: a
formatter that composes its own sentences about a student is a second adviser, and
this file is a pipe.
"""

from __future__ import annotations

import re
from typing import Any

from .transport import TELEGRAM_MAX_MESSAGE_CHARS

#: Below Telegram's hard 4096 so that a chunk counter and a leading newline can be
#: added without any chance of pushing a message over the API limit.
SAFE_CHUNK_CHARS = 3500

#: Characters Telegram's MarkdownV2 treats as syntax. Reserved for a caller that
#: opts into markup; the delivery path sends plain text.
_MARKDOWN_V2_SPECIALS = r"_*[]()~`>#+-=|{}.!\\"


def escape_markdown_v2(text: str) -> str:
    """Escape every MarkdownV2 metacharacter.

    Provided so that a future markup mode has one correct implementation to reach
    for rather than an ad-hoc one per call site. Escaping the backslash is part of
    the set and has to happen inside the same pass, not before it, or a literal
    backslash in the answer eats the escape of whatever follows.
    """
    return re.sub(f"([{re.escape(_MARKDOWN_V2_SPECIALS)}])", r"\\\1", str(text or ""))


def _split_oversized(block: str, limit: int) -> list[str]:
    """Break one block that is itself longer than a whole message.

    Prefers a line break, then a space, and only cuts mid-token when a single
    unbroken run genuinely exceeds the limit — an Arabic answer has no such runs,
    but a pasted URL does.
    """
    out: list[str] = []
    remaining = block
    while len(remaining) > limit:
        window = remaining[:limit]
        cut = window.rfind("\n")
        if cut <= 0:
            cut = window.rfind(" ")
        if cut <= 0:
            cut = limit
        out.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        out.append(remaining)
    return out


def split_message(text: str, *, limit: int = SAFE_CHUNK_CHARS) -> list[str]:
    """Split an answer into deliverable chunks, on real boundaries.

    Paragraphs are kept together where they fit, because a paragraph is the unit
    the answer was written in. An empty or blank answer yields no chunks at all
    rather than one empty message — Telegram rejects empty text, and a rejected
    send is indistinguishable from an outage in the logs.
    """
    text = str(text or "").strip()
    if not text:
        return []
    limit = max(1, min(int(limit), TELEGRAM_MAX_MESSAGE_CHARS))
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current = ""
    for block in text.split("\n\n"):
        block = block.strip("\n")
        if not block:
            continue
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        if len(block) <= limit:
            current = block
            continue
        pieces = _split_oversized(block, limit)
        chunks.extend(pieces[:-1])
        current = pieces[-1] if pieces else ""
    if current:
        chunks.append(current)
    return [c for c in (c.strip() for c in chunks) if c]


def render_answer(
    *,
    answer: str,
    citations: Any = None,
    web_url: str = "",
    has_presentation: bool = False,
    language: str = "ar",
    limit: int = SAFE_CHUNK_CHARS,
) -> list[str]:
    """The student-visible answer, as the messages that will actually be sent.

    `citations` are the SAVED citation rows, so what is shown is what was stored —
    the same property the web thread relies on. They are appended as one block and
    that block is never split away from the end of the answer: a source list is
    only meaningful attached to the claim it supports.

    `has_presentation` is the timetable/graduation card the web chat renders. The
    adviser's prompt deliberately keeps the prose short *because* that card exists,
    so on a text-only channel the answer alone is incomplete. Rather than rebuild
    the card in chat messages, the student is pointed at the screen that already
    draws it.
    """
    body = str(answer or "").strip()
    if not body:
        return []

    language_code = _language_code(language)
    tail_parts: list[str] = []

    rendered_citations = _citation_lines(citations, language=language_code)
    if rendered_citations:
        source_heading = "المصادر:" if language_code == "ar" else "Sources:"
        tail_parts.append(source_heading + "\n" + "\n".join(rendered_citations))

    if has_presentation and web_url:
        platform_heading = (
            "عرض الخطة والتفاصيل الكاملة على المنصة:"
            if language_code == "ar"
            else "View the full plan and details on the platform:"
        )
        tail_parts.append(f"{platform_heading}\n{web_url}")

    tail = "\n\n".join(tail_parts)

    # The body is split first, then the tail is appended to the last chunk if it
    # fits, or sent as its own final chunk if it does not. Either way the tail
    # stays whole and stays last — the two properties that stop a disclaimer being
    # cut in half or stranded in the middle.
    chunks = split_message(body, limit=limit)
    if not tail:
        return chunks
    if chunks and len(chunks[-1]) + len(tail) + 2 <= limit:
        chunks[-1] = f"{chunks[-1]}\n\n{tail}"
        return chunks
    return [*chunks, *split_message(tail, limit=limit)]


def _language_code(language: Any) -> str:
    """Reduce the transport's deterministic language decision to a closed value."""

    return "ar" if str(language or "").strip().lower() in {"ar", "arabic"} else "en"


def _citation_lines(citations: Any, *, language: str = "ar") -> list[str]:
    """One line per source, from the stored rows.

    The policy id is deliberately absent. It is the machine half of the citation
    contract — the half `validate_citations` checks — and a student reading
    «TU.WITHDRAWAL.MAXIMUM» on their phone learns nothing they can act on.
    """
    if not citations:
        return []
    language_code = _language_code(language)
    lines: list[str] = []
    seen: set[str] = set()
    for citation in citations:
        title = str(getattr(citation, "document_title", "") or "").strip()
        page = str(getattr(citation, "page", "") or "").strip()
        edition = str(getattr(citation, "edition", "") or "").strip()
        if not title:
            continue
        parts = [title]
        if edition:
            parts.append(edition)
        if page:
            parts.append(f"ص {page}" if language_code == "ar" else f"p. {page}")
        separator = "، " if language_code == "ar" else ", "
        line = "• " + separator.join(parts)
        if line in seen:
            continue
        seen.add(line)
        lines.append(line)
    return lines


__all__ = [
    "SAFE_CHUNK_CHARS",
    "escape_markdown_v2",
    "render_answer",
    "split_message",
]

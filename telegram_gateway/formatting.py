"""Getting an adviser's answer onto a phone without changing what it says.

Three problems, and the third is the one that bites.

**Markup.** The answer is written by a model and contains course codes, policy
references and bracketed ids. Sent under any Telegram parse mode those characters
become syntax. The gateway sends plain text and sets no `parse_mode` at all. A
small Telegram-only normaliser unwraps balanced strong-emphasis delimiters such
as ``**heading**`` so model formatting hints do not appear as literal stars. It
does not interpret links, code, single stars or unmatched delimiters.

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
_ABSOLUTE_URL = re.compile(r"https?://[^\s<>\"']+", flags=re.IGNORECASE)
_URL_TRAILING_PUNCTUATION = frozenset(".,;:!?،؛؟)]}")


def escape_markdown_v2(text: str) -> str:
    """Escape every MarkdownV2 metacharacter.

    Provided so that a future markup mode has one correct implementation to reach
    for rather than an ad-hoc one per call site. Escaping the backslash is part of
    the set and has to happen inside the same pass, not before it, or a literal
    backslash in the answer eats the escape of whatever follows.
    """
    return re.sub(f"([{re.escape(_MARKDOWN_V2_SPECIALS)}])", r"\\\1", str(text or ""))


def _is_escaped(text: str, position: int) -> bool:
    """Whether the character at ``position`` follows an odd backslash run."""

    backslashes = 0
    cursor = position - 1
    while cursor >= 0 and text[cursor] == "\\":
        backslashes += 1
        cursor -= 1
    return bool(backslashes % 2)


def _strong_marker_positions(text: str, *, protected: list[bool]) -> list[int]:
    """Offsets of unescaped ``**`` runs, never a slice of a longer star run."""

    positions: list[int] = []
    cursor = 0
    while cursor < len(text) - 1:
        position = text.find("**", cursor)
        if position < 0:
            break
        before_is_star = position > 0 and text[position - 1] == "*"
        after = position + 2
        after_is_star = after < len(text) and text[after] == "*"
        if (
            not before_is_star
            and not after_is_star
            and not _is_escaped(text, position)
            and not protected[position]
            and not protected[position + 1]
        ):
            positions.append(position)
        cursor = position + 2
    return positions


def _matching_inline_backtick_run(
    text: str,
    delimiter: str,
    start: int,
) -> tuple[int, int] | None:
    """Find an exact inline-code delimiter and return its complete run."""

    position = text.find("`", start)
    while position >= 0:
        run_end = position + 1
        while run_end < len(text) and text[run_end] == "`":
            run_end += 1
        run_length = run_end - position
        # Backslashes are literal inside a Markdown code span; they do not escape
        # its closing delimiter.
        if run_length == len(delimiter):
            return position, run_end
        position = text.find("`", run_end)
    return None


def _has_fence_indent(text: str, position: int) -> bool:
    line_start = text.rfind("\n", 0, position) + 1
    indent = text[line_start:position]
    return len(indent) <= 3 and not indent.strip(" ")


def _is_fence_opener(text: str, opening: int, run_end: int) -> bool:
    if run_end - opening < 3 or not _has_fence_indent(text, opening):
        return False
    line_end = text.find("\n", run_end)
    if line_end < 0:
        line_end = len(text)
    return "`" not in text[run_end:line_end]


def _matching_fence_run(text: str, minimum: int, start: int) -> tuple[int, int] | None:
    """Find a CommonMark-style closing fence, never a backtick run in code."""

    position = text.find("`", start)
    while position >= 0:
        run_end = position + 1
        while run_end < len(text) and text[run_end] == "`":
            run_end += 1
        line_end = text.find("\n", run_end)
        if line_end < 0:
            line_end = len(text)
        trailing = text[run_end:line_end]
        if (
            not _is_escaped(text, position)
            and run_end - position >= minimum
            and _has_fence_indent(text, position)
            and not trailing.strip(" \t\r")
        ):
            return position, run_end
        position = text.find("`", run_end)
    return None


def _matched_code_mask(text: str) -> list[bool]:
    """Mark matched backtick regions whose literal contents must never change."""

    protected = [False] * len(text)
    cursor = 0
    while cursor < len(text):
        opening = text.find("`", cursor)
        if opening < 0:
            break
        run_end = opening + 1
        while run_end < len(text) and text[run_end] == "`":
            run_end += 1
        if _is_escaped(text, opening):
            cursor = run_end
            continue
        delimiter = text[opening:run_end]
        is_fence = _is_fence_opener(text, opening, run_end)
        closing = (
            _matching_fence_run(text, len(delimiter), run_end)
            if is_fence
            else _matching_inline_backtick_run(text, delimiter, run_end)
        )
        if closing is None:
            if is_fence:
                protected[opening:] = [True] * (len(text) - opening)
                break
            cursor = run_end
            continue
        _closing_start, closing_end = closing
        protected[opening:closing_end] = [True] * (closing_end - opening)
        cursor = closing_end
    return protected


def _protected_literal_mask(text: str) -> list[bool]:
    """Protect code and absolute URLs from any output-only punctuation cleanup."""

    protected = _matched_code_mask(text)
    for match in _ABSOLUTE_URL.finditer(text):
        start, end = match.span()
        closing = text.rfind("**", start, end)
        line_start = text.rfind("\n", 0, start) + 1
        opening = text.rfind("**", line_start, start)
        trailing = text[closing + 2 : end] if closing >= 0 else ""
        has_outer_closing = (
            opening >= line_start
            and closing >= start
            and not _is_escaped(text, opening)
            and all(character in _URL_TRAILING_PUNCTUATION for character in trailing)
            and _has_prose_boundaries(text, opening, closing, protected=protected)
        )
        if has_outer_closing:
            # Preserve the URL itself while leaving its surrounding prose-level
            # markers visible to the normaliser.
            protected[start:closing] = [True] * (closing - start)
            continue
        protected[start:end] = [True] * (end - start)
    return protected


def _has_prose_boundaries(
    text: str,
    opening: int,
    closing: int,
    *,
    protected: list[bool],
) -> bool:
    """Reject token-internal/path delimiters that only resemble emphasis."""

    before = text[opening - 1] if opening else ""
    after_position = closing + 2
    after = text[after_position] if after_position < len(text) else ""
    valid_before = not before or before.isspace() or before in "([{\"'“‘«،؛؟"
    valid_after = not after or after.isspace() or after in ".,;:!?،؛؟)]}\"'”’»"
    content_start = text[opening + 2]
    content_end = text[closing - 1]
    invalid_content_edges = "_/\\*"
    has_unprotected_star = any(
        text[position] == "*" and not protected[position]
        for position in range(opening + 2, closing)
    )
    valid_content = (
        not content_start.isspace()
        and not content_end.isspace()
        and content_start not in invalid_content_edges
        and content_end not in invalid_content_edges
        and not has_unprotected_star
    )
    return valid_before and valid_after and valid_content


def _unwrap_strong_markers(text: str) -> str:
    """Remove paired model strong markers without interpreting other markup.

    Repeating passes handle nested strong spans while every successful
    substitution makes the string shorter. Backtick code regions, escaped,
    unmatched, multiline and whitespace-only pairs remain literal so ordinary
    prose and course identifiers are never guessed at.
    """

    rendered = str(text or "")
    while True:
        protected = _protected_literal_mask(rendered)
        positions = _strong_marker_positions(rendered, protected=protected)
        parts: list[str] = []
        cursor = 0
        marker_index = 0
        changed = False
        while marker_index + 1 < len(positions):
            opening = positions[marker_index]
            closing = positions[marker_index + 1]
            content = rendered[opening + 2 : closing]
            if (
                content
                and not content[0].isspace()
                and not content[-1].isspace()
                and "\n" not in content
                and "\r" not in content
                and _has_prose_boundaries(
                    rendered,
                    opening,
                    closing,
                    protected=protected,
                )
            ):
                parts.append(rendered[cursor:opening])
                parts.append(content)
                cursor = closing + 2
                marker_index += 2
                changed = True
                continue
            marker_index += 1
        if not changed:
            break
        parts.append(rendered[cursor:])
        rendered = "".join(parts)
    return rendered


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
    body = _unwrap_strong_markers(str(answer or "").strip())
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

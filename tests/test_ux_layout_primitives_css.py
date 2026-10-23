"""Static guards for the design system's layout primitives (stage 3, C2).

- A `@keyframes` rule is a top-level (or @media-level) statement: written
  after a selector (`[dir="rtl"] @keyframes …`) it is not valid CSS, and the
  browser drops it silently (SW-27: the advisor drawer swept across the
  page in Arabic).
- A toolbar row is never given a fixed block size or clipped: it wraps, and
  with the WCAG 1.4.12 text spacing it must grow (the plan's L30 fix was
  rejected for exactly this; tests/test_ux_layout_primitives_browser.py
  measures the effect).
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CSS = sorted((ROOT / "static" / "css").glob("*.css"))
COMMENT = re.compile(r"/\*.*?\*/", re.S)
RULE = re.compile(r"([^{}]+)\{([^{}]*)\}")


def _text(path: Path) -> str:
    return COMMENT.sub(lambda m: " " * len(m.group(0)), path.read_text(encoding="utf-8"))


def test_every_keyframes_rule_starts_a_statement() -> None:
    found = []
    for path in CSS:
        text = _text(path)
        for match in re.finditer(r"@keyframes\b", text):
            before = text[: match.start()].rstrip()
            if before and before[-1] not in "{};":
                line = text.count("\n", 0, match.start()) + 1
                found.append(f"{path.name}:{line}: {before.splitlines()[-1].strip()} @keyframes")
    assert found == []


def test_a_toolbar_never_gets_a_fixed_block_size_or_a_clip() -> None:
    fixed = re.compile(
        r"(?:^|;)\s*(?:block-size|height|max-block-size|max-height|overflow(?:-[xy])?)\s*:", re.M
    )
    found = []
    for path in CSS:
        for selector, body in RULE.findall(_text(path)):
            targets = [part.strip() for part in selector.split(",")]
            # A rule that styles the toolbar itself (not something inside it).
            if any(
                re.search(r"\.ux-toolbar(?:[.:\[][^\s>+~]*)?$", target) for target in targets
            ) and fixed.search(body):
                found.append(f"{path.name}: {selector.strip()} {{{body.strip()}}}")
    assert found == []

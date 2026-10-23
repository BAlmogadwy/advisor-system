"""The stylesheets never paint text in a colour that fails, and never shrink the
sidebar under the type floor (stage 3, C1; docs/stage3/C1.md).

`--teal` (#0A8E6E, also known as `--brand` and `--success`) and `--amber` /
`--warning` are fills, not text colours: as text on a light card teal is
4.11:1 and amber 3.2:1, and white text on a teal fill is 4.11:1 (light) and
3.04:1 (dark). `global.css` defines the passing pair for each job
(`--text-brand`, `--btn-primary-bg` / `--btn-primary-ink`, the `--sev-*`
inks), so a stylesheet that reaches for a fill as text again fails here, by
file, selector and line. `--t5` is the same kind of token: a stroke under
`--t4` (2.2:1 as text), so text takes `--t4`.

Two stylesheets are left as they are, on purpose:
- `timetable-workspace-split.css`: split view forces a dark palette of its own
  (stage 4 rethemes it). Its token block gives `--text-brand` its dark value
  instead, so the shared rules it renders stay readable.
- `timetable-mri.css`: the owner's decision of 2026-10-23, no work on MRI.
Their counts may only fall.
"""

from __future__ import annotations

import pathlib
import re

import pytest

CSS_DIR = pathlib.Path(__file__).resolve().parents[1] / "static" / "css"

#: file -> the most text declarations in a fill colour it may keep.
EXCLUDED = {"timetable-workspace-split.css": 31, "timetable-mri.css": 3}

#: The brand and success fills, by token (with or without a fallback) or as the
#: teal literals themselves: a background in one of these, under white text.
BRAND_FILL = re.compile(
    r"var\(--(?:teal|teal-bright|teal-deep|teal-pale|brand|brand-dark|success)\s*[,)]"
    r"|#(?:0a8e6e|0ca87e|2ec9a0|12a259)\b",
    re.I,
)
WHITE = re.compile(r"^(?:#fff|#ffffff|white)\b", re.I)
#: A fill token used as text: the teal family and its aliases (--brand and
#: --success are #0A8E6E too: 4.11:1 on white; --teal-bright 3.06:1; --teal-pale
#: 1.9:1), and amber/warning (3.2:1 and 4.13:1 on the page). With or without a
#: fallback. (--brand-dark is --text-brand's value in both themes, and passes.)
#: And --t5, the tone under --t4: 2.2:1 on a light card, 2.4:1 on a dark one;
#: it is for disabled and decorative strokes, never for text.
#: Also the literal values those tokens had or have, where a dark rule copied one
#: in (#7a7e92 was --t4 dark: 3.99-4.39:1).
FILL_AS_TEXT = re.compile(
    r"var\(--(?:teal|teal-bright|teal-pale|teal-deep|brand|success|amber|warning|t5)\s*[,)]"
    r"|^#(?:0a8e6e|0ca87e|d97706|9a6a08|a8a8be|505468|7a7e92)\b",
    re.I,
)


def _rules(css: str):
    """(media conditions, selector, declarations, line) for every style rule, in
    order. Comments are blanked first, keeping line numbers."""
    css = re.sub(r"/\*.*?\*/", lambda m: re.sub(r"[^\n]", " ", m.group(0)), css, flags=re.S)
    stack: list[str] = []
    pos = 0
    head_start = 0
    while pos < len(css):
        ch = css[pos]
        if ch == "{":
            head = css[head_start:pos].strip()
            if head.startswith("@"):
                stack.append(head)
                head_start = pos + 1
            else:
                end = css.index("}", pos)
                line = css.count("\n", 0, pos) + 1
                yield tuple(stack), " ".join(head.split()), css[pos + 1 : end], line
                pos = end
                head_start = end + 1
        elif ch == "}":
            if stack:
                stack.pop()
            head_start = pos + 1
        pos += 1


def _declarations(body: str):
    for part in body.split(";"):
        if ":" in part:
            prop, value = part.split(":", 1)
            yield prop.strip().lower(), value.strip()


def _sheets():
    return sorted(CSS_DIR.glob("*.css"))


def _fill_as_text(path: pathlib.Path) -> list[str]:
    found = []
    for _, selector, body, line in _rules(path.read_text(encoding="utf-8")):
        for prop, value in _declarations(body):
            if prop == "color" and FILL_AS_TEXT.search(value):
                found.append(f"{path.name}:{line} {selector} {{ color: {value} }}")
    return found


def test_the_rule_parser_sees_media_blocks_and_lines() -> None:
    css = "/* a\n */\n.a { color: red; }\n@media (max-width: 9px) {\n  .b { font-size: 1px }\n}\n"
    rules = list(_rules(css))
    assert rules == [
        ((), ".a", " color: red; ", 3),
        (("@media (max-width: 9px)",), ".b", " font-size: 1px ", 5),
    ]


@pytest.mark.parametrize("path", _sheets(), ids=lambda p: p.name)
def test_no_stylesheet_uses_a_fill_colour_as_text(path: pathlib.Path) -> None:
    found = _fill_as_text(path)
    if path.name in EXCLUDED:
        assert len(found) <= EXCLUDED[path.name], found
    else:
        assert found == [], (
            "--teal and --amber are fills: text takes --text-brand or a --sev-* ink\n"
            + "\n".join(found)
        )


def test_the_excluded_sheets_are_still_there() -> None:
    # An exclusion for a file that is gone (or renamed) would hide nothing and
    # silently let its successor through.
    for name in EXCLUDED:
        assert (CSS_DIR / name).is_file(), name


@pytest.mark.parametrize("path", _sheets(), ids=lambda p: p.name)
def test_no_rule_puts_white_text_on_a_brand_fill(path: pathlib.Path) -> None:
    if path.name in EXCLUDED:
        pytest.skip("left as it is (see the module docstring)")
    found = []
    for _, selector, body, line in _rules(path.read_text(encoding="utf-8")):
        decls = dict(_declarations(body))
        fill = decls.get("background") or decls.get("background-color") or ""
        ink = (decls.get("color") or "").replace("!important", "").strip()
        if BRAND_FILL.search(fill) and "color-mix" not in fill and WHITE.match(ink):
            found.append(f"{path.name}:{line} {selector}")
    assert found == [], (
        "white on a brand fill is 4.11:1 (light) / 3.04:1 (dark): use "
        "--btn-primary-bg with --btn-primary-ink\n" + "\n".join(found)
    )


def _global_rules():
    return list(_rules((CSS_DIR / "global.css").read_text(encoding="utf-8")))


def _rule(selector: str, *, media: tuple[str, ...] = ()) -> dict[str, str]:
    hits = [
        dict(_declarations(b)) for m, s, b, _ in _global_rules() if s == selector and m == media
    ]
    assert hits, f"no rule {selector!r} under {media}"
    merged: dict[str, str] = {}
    for h in hits:
        merged.update(h)
    return merged


def test_the_skip_link_is_the_primary_pair_and_keeps_a_box_in_forced_colours() -> None:
    rule = _rule(".skip-link")
    assert rule["background"] == "var(--btn-primary-bg)"
    assert rule["color"] == "var(--btn-primary-ink)"
    forced = _rule(".skip-link", media=("@media (forced-colors: active)",))
    assert re.match(r"\d+px solid currentColor", forced["border"]), forced


SIDEBAR_SIZES = {
    ".logo-sub": "var(--text-2xs)",
    ".nav-lbl": "var(--text-2xs)",
    ".nav-badge": "var(--text-2xs)",
    ".avatar": "var(--text-2xs)",
    ".u-rl": "var(--text-2xs)",
    ".u-nm": "var(--text-xs)",
    ".nav-a": "var(--text-sm)",
    ".sidebar .form-select": "var(--text-xs)",
}


def test_the_sidebar_text_sizes_are_type_tokens() -> None:
    # Each step carries a px floor (--text-2xs is max(11px, …)); a rem or clamp
    # alone rendered 6.5–10.5px on the fluid 14px root of a phone.
    for selector, size in SIDEBAR_SIZES.items():
        assert _rule(selector)["font-size"] == size, selector


def test_no_breakpoint_shrinks_the_sidebar_text() -> None:
    # The ≤1200px and ≤992px blocks shrank them again (0.48rem, 0.46rem …):
    # they keep their compact geometry, never a smaller size.
    shrinkable = set(SIDEBAR_SIZES) | {".s-lang select"}
    found = []
    for media, selector, body, line in _global_rules():
        if not media:
            continue
        if {part.strip() for part in selector.split(",")} & shrinkable and "font-size" in dict(
            _declarations(body)
        ):
            found.append(f"global.css:{line} {media[-1]} {selector}")
    assert found == [], "\n".join(found)


def test_the_light_theme_declares_its_colour_scheme() -> None:
    assert _rule('html[data-theme="light"]')["color-scheme"] == "light"


def test_every_placeholder_in_the_design_system_reads_t4() -> None:
    base = _rule("::placeholder")
    assert base["color"] == "var(--t4)" and base["opacity"] == "1"
    found = []
    for _, selector, body, line in _global_rules():
        if "::placeholder" not in selector:
            continue
        colour = dict(_declarations(body)).get("color")
        if colour and colour.replace("!important", "").strip() not in (
            "var(--t4)",
            "var(--muted-light)",  # an alias of --t4 (global.css :root)
        ):
            found.append(f"global.css:{line} {selector} {{ color: {colour} }}")
    assert found == [], "\n".join(found)


def test_status_pills_take_severity_inks_and_lie_flat_in_a_toolbar() -> None:
    for pill, sev in (("g", "ok"), ("r", "critical"), ("a", "warning"), ("b", "info")):
        assert _rule(f".pill-{pill}")["--pill-ink"] == f"var(--sev-{sev})"
    shared = _rule(".pill-g, .pill-r, .pill-a, .pill-b")
    assert shared["color"] == "var(--pill-ink)"
    assert re.fullmatch(
        r"color-mix\(in srgb, var\(--pill-ink\) \d+%, var\(--card\)\)", shared["background"]
    ), shared
    flat = _rule('.ux-toolbar .pill-status, html[data-theme="dark"] .ux-toolbar .pill-status')
    assert flat["box-shadow"] == "none !important"
    assert _rule(".ux-toolbar .pill-status::before")["content"] == "none"

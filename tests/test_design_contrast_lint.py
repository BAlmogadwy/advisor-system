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
#: A white ink: the literal, or the primary pair's ink (white in light).
WHITE = re.compile(r"^(?:#fff|#ffffff|white|var\(--btn-primary-ink\))(?![\w-])", re.I)
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


def _fill(decls: dict[str, str]) -> str:
    return " ".join(
        decls.get(p, "") for p in ("background", "background-color", "background-image")
    )


def _ink(decls: dict[str, str]) -> str:
    return (decls.get("color") or "").replace("!important", "").strip()


def _is_brand_fill(fill: str) -> bool:
    # A wash (color-mix) is a tint, not a fill: the tokens test pins text on it.
    return bool(BRAND_FILL.search(fill)) and "color-mix" not in fill


@pytest.mark.parametrize("path", _sheets(), ids=lambda p: p.name)
def test_no_rule_puts_white_text_on_a_brand_fill(path: pathlib.Path) -> None:
    if path.name in EXCLUDED:
        pytest.skip("left as it is (see the module docstring)")
    found = []
    for _, selector, body, line in _rules(path.read_text(encoding="utf-8")):
        decls = dict(_declarations(body))
        if _is_brand_fill(_fill(decls)) and WHITE.match(_ink(decls)):
            found.append(f"{path.name}:{line} {selector}")
    assert found == [], (
        "white on a brand fill is 4.11:1 (light) / 3.04:1 (dark): use "
        "--btn-primary-bg with --btn-primary-ink\n" + "\n".join(found)
    )


# ── The same pair, across the cascade ───────────────────────────────────────
# A rule for a state (`.neu-btn:hover`), a context (`.sa-main .va-avatar`) or a
# theme (`html[data-theme="dark"] .x`) that repaints only the fill keeps the
# white ink an earlier rule gave the element: the login button's hover was
# white on --teal-bright, 3.04:1, and no single rule said so.
#
# States are simple selectors like any other: `.x` (any state) covers
# `.x:hover`, but `.x:focus-visible` does not, since a hovered `.x` need not be
# focused. The theme prefix is dropped from both sides: a white ink set for
# one theme reaches a fill rule that applies in every theme.

_THEME = re.compile(r"""^(?:html)?\[data-theme=["']?(?:dark|light)["']?\]$""")
_SIMPLE = re.compile(r"::?[\w-]+(?:\([^)]*\))?|[.#][\w-]+|\[[^\]]*\]|^[\w*-]+")


def _compounds(selector: str) -> list[frozenset[str]]:
    """One selector (no commas) as its compounds, each the set of its simple
    selectors, without the theme prefix."""
    out = []
    for token in re.split(r"\s*[>+~]\s*|\s+", selector.strip()):
        if token and not _THEME.match(token):
            out.append(frozenset(_SIMPLE.findall(token)))
    return out


def _covers(general: list[frozenset[str]], specific: list[frozenset[str]]) -> bool:
    """Whether every element `specific` matches is matched by `general` too (a
    sufficient test: its subject is a subset of the other's subject, and its
    ancestors a subsequence of the other's ancestors)."""
    if not general or not specific or not general[-1] <= specific[-1]:
        return False
    ancestors = iter(specific[:-1])
    return all(any(need <= have for have in ancestors) for need in general[:-1])


def _paints_text(compound: frozenset[str], decls: dict[str, str]) -> bool:
    # A pseudo-element with no content (a dot, a ring) has no text to read.
    pseudo = any(s.startswith("::") or s in (":before", ":after") for s in compound)
    return not pseudo or decls.get("content", "").strip() not in ("", "''", '""', "none")


def _white_on_brand_by_cascade(sheets: dict[str, str]) -> list[str]:
    rules = [
        (name, selector, dict(_declarations(body)), line)
        for name, css in sheets.items()
        for _, selector, body, line in _rules(css)
    ]
    white = [
        _compounds(part)
        for _, selector, decls, _ in rules
        if WHITE.match(_ink(decls))
        for part in selector.split(",")
        # the primary ink is white in light only; in dark it is near-black
        if not ("--btn-primary-ink" in _ink(decls) and 'data-theme="dark"' in part)
    ]
    found = []
    for name, selector, decls, line in rules:
        if "color" in decls or not _is_brand_fill(_fill(decls)):
            continue  # a rule with its own ink is judged on its own (above)
        for part in selector.split(","):
            target = _compounds(part)
            if not target or not _paints_text(target[-1], decls):
                continue
            if any(_covers(ink, target) for ink in white):
                found.append(f"{name}:{line} {part.strip()}")
    return found


def test_the_cascade_lint_sees_states_contexts_and_themes() -> None:
    css = """
.b { background: var(--btn-primary-bg); color: var(--btn-primary-ink); }
.b:hover { background: var(--teal-bright); }
.wrap > .b.big:focus-visible { background: var(--teal); }
.side .b { background: linear-gradient(135deg, var(--brand), var(--teal)); }
html[data-theme="dark"] .b:active { background: #0ca87e; }
.b::after { content: ''; background: var(--teal); }
.b::before { content: "!"; background: var(--teal); }
.b.calm:hover { background: var(--teal); color: var(--t1); }
.b:hover { background: color-mix(in srgb, var(--teal) 8%, transparent); }
.c:hover { background: var(--teal); }
.b-other:hover { background: var(--teal); }
.e:focus-visible { background: var(--btn-primary-bg); color: #fff; }
.e:hover { background: var(--teal); }
html[data-theme="dark"] .d { background: var(--btn-primary-bg); color: #fff; }
.d:hover { background: var(--teal-bright); }
html[data-theme="dark"] .f { background: var(--btn-primary-bg); color: var(--btn-primary-ink); }
.f:hover { background: var(--teal-bright); }
"""
    assert _white_on_brand_by_cascade({"t.css": css}) == [
        "t.css:3 .b:hover",
        "t.css:4 .wrap > .b.big:focus-visible",
        "t.css:5 .side .b",
        't.css:6 html[data-theme="dark"] .b:active',
        "t.css:8 .b::before",
        "t.css:16 .d:hover",
    ]
    # A context the white rule needs is a context the fill rule must have too.
    assert not _covers(_compounds(".dlg .b"), _compounds(".b:hover"))
    assert _covers(_compounds(".dlg .b"), _compounds(".page .dlg .x .b.primary:hover"))


def test_no_state_or_context_rule_puts_a_brand_fill_under_a_white_ink() -> None:
    sheets = {
        path.name: path.read_text(encoding="utf-8")
        for path in _sheets()
        if path.name not in EXCLUDED
    }
    found = _white_on_brand_by_cascade(sheets)
    assert found == [], (
        "this rule repaints the fill under an ink another rule makes white: use "
        "--btn-primary-bg, or --btn-primary-hover for a hover or pressed state\n" + "\n".join(found)
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


# ── Inline styles in templates and scripts ──────────────────────────────────
# C1 changes stylesheets; an inline style is a page's own. White on a brand fill
# must not come back anywhere (section planning's "Save" was the last, 4.11:1).
# Text in a fill colour is left in seven script lines for D6 (docs/stage3/C1.md,
# "What C1 deliberately left"), and may only fall:
#   page-planner.js: the importance chip (--teal on --teal-dim, --warning);
#   page-timetable-workspace.js: the instructor outcome's heading, the classic
#   Optimise result (unresolved count, zero scores, its chip), Reserve Pressure.

ROOT = CSS_DIR.parents[1]
#: MRI (owner), split (stage 4), and the error pages (their own tokens; they do
#: not load global.css).
INLINE_EXCLUDED = ("mri", "split", "404.html", "500.html")
INLINE_FILL_AS_TEXT_CAP = 7
INLINE_FILL_AS_TEXT = re.compile(
    r"(?<![\w-])color:\s*var\(--(?:teal|teal-bright|teal-pale|teal-deep|brand|success|amber"
    r"|warning|t5)\s*[,)]",
    re.I,
)
INLINE_BRAND_FILL = re.compile(
    r"background(?:-color)?:\s*(?:var\(--(?:teal|teal-bright|teal-deep|brand|success)\s*[,)]"
    r"|#(?:0a8e6e|0ca87e)\b)",
    re.I,
)
INLINE_WHITE = re.compile(r"(?<![\w-])color:\s*(?:#fff\b|#ffffff\b|white\b)", re.I)


def _inline_sources() -> list[pathlib.Path]:
    found = [
        *(ROOT / "core" / "templates").rglob("*.html"),
        *(ROOT / "telegram_gateway" / "templates").rglob("*.html"),
        *(ROOT / "static" / "js").glob("*.js"),
    ]
    return sorted(p for p in found if not any(x in p.name for x in INLINE_EXCLUDED))


def _inline_lines(pattern: re.Pattern[str], also: re.Pattern[str] | None = None) -> list[str]:
    hits = []
    for path in _inline_sources():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line) and (also is None or also.search(line)):
                hits.append(f"{path.relative_to(ROOT).as_posix()}:{number}")
    return hits


def test_the_inline_scan_reads_the_templates_and_scripts() -> None:
    names = {p.name for p in _inline_sources()}
    assert {"base.html", "section_planning.html", "page-timetable-workspace.js"} <= names
    assert not {"page-timetable-mri.js", "timetable_workspace_split.html"} & names


def test_no_inline_style_puts_white_text_on_a_brand_fill() -> None:
    assert _inline_lines(INLINE_BRAND_FILL, INLINE_WHITE) == []


def test_inline_text_in_a_fill_colour_only_falls() -> None:
    found = _inline_lines(INLINE_FILL_AS_TEXT)
    assert len(found) <= INLINE_FILL_AS_TEXT_CAP, (
        "a new inline style paints text in a fill colour: use --text-brand or a --sev-* ink\n"
        + "\n".join(found)
    )

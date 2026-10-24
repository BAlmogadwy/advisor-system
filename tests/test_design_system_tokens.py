"""The design system's text and fill tokens pass WCAG contrast in both themes.

docs/TIMETABLE-WORKSPACE-UI.md, section 3.2. Each pair below is a decision made
in `static/css/global.css`: secondary text (`--t4`) used to be 4.18:1 on a
light card and 4.39:1 on a dark one; white on the brand teal was 4.11:1 and
3.04:1. The tokens are read from the stylesheet itself — the light `:root`
block and the dark `html[data-theme="dark"]` block — so a later edit to a value
fails here, with the pair it broke, rather than on a screen.
"""

from __future__ import annotations

import pathlib
import re

import pytest

CSS = (pathlib.Path(__file__).resolve().parents[1] / "static/css/global.css").read_text(
    encoding="utf-8"
)


def _block(selector: str) -> str:
    start = CSS.index(selector + " {")
    return CSS[start : CSS.index("\n}", start)]


THEMES = {
    "light": _block("\n:root"),
    "dark": _block('\nhtml[data-theme="dark"]'),
}


def _hex(theme: str, token: str) -> str:
    found = re.search(rf"{re.escape(token)}:\s*(#[0-9a-fA-F]{{6}})\b", THEMES[theme])
    assert found, f"{token} is not a plain hex colour in the {theme} block"
    return found.group(1)


def _luminance(colour: str) -> float:
    def channel(value: int) -> float:
        c = value / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (int(colour[i : i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def _ratio(a: str, b: str) -> float:
    la, lb = _luminance(a), _luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


#: Text tokens and the surfaces they are read on: 4.5:1 for text of any size.
TEXT_ON_SURFACES = [
    (token, surface)
    for token in (
        "--t3",
        "--t4",
        "--text-brand",
        "--sev-critical",
        "--sev-warning",
        "--sev-info",
        "--sev-ok",
    )
    for surface in ("--card", "--bg", "--surface")
]


@pytest.mark.parametrize("theme", sorted(THEMES))
@pytest.mark.parametrize(("token", "surface"), TEXT_ON_SURFACES)
def test_text_tokens_clear_4_5_on_every_surface(theme: str, token: str, surface: str) -> None:
    ratio = _ratio(_hex(theme, token), _hex(theme, surface))
    assert ratio >= 4.5, f"{theme} {token} on {surface}: {ratio:.2f}:1"


@pytest.mark.parametrize("theme", sorted(THEMES))
def test_the_primary_fill_and_its_ink_clear_4_5_including_on_hover(theme: str) -> None:
    ink = _hex(theme, "--btn-primary-ink") if theme == "dark" else "#ffffff"
    for fill in ("--btn-primary-bg", "--btn-primary-hover"):
        ratio = _ratio(_hex(theme, fill), ink)
        assert ratio >= 4.5, f"{theme} {fill} under its ink: {ratio:.2f}:1"


@pytest.mark.parametrize("theme", sorted(THEMES))
def test_the_primary_fill_is_a_visible_edge_on_its_card(theme: str) -> None:
    # A filled button needs no ring: its fill is the boundary (WCAG 1.4.11).
    ratio = _ratio(_hex(theme, "--btn-primary-bg"), _hex(theme, "--card"))
    assert ratio >= 3, f"{theme} primary fill on card: {ratio:.2f}:1"


def test_the_two_secondary_text_levels_stay_distinct() -> None:
    # --t4 must pass on its own, and still read as a step below --t3.
    for theme in THEMES:
        t3, t4, card = (_hex(theme, name) for name in ("--t3", "--t4", "--card"))
        assert _ratio(t3, card) > _ratio(t4, card), theme


def test_the_small_type_steps_never_go_under_their_px_floor() -> None:
    # The root size is fluid (14px at 375px wide), so a rem-only floor renders
    # under itself on a phone: the two smallest steps carry a px floor.
    root = THEMES["light"]
    assert re.search(r"--text-2xs:\s*max\(11px,", root)
    assert re.search(r"--text-xs:\s*max\(12px,", root)


# ── Stage 3, C1 (docs/stage3/C1.md): the pairs the remap now relies on ───────


def _mix(ink: str, percent: float, base: str) -> str:
    """`color-mix(in srgb, ink percent%, base)` as an opaque hex colour."""
    a = [int(ink[i : i + 2], 16) for i in (1, 3, 5)]
    b = [int(base[i : i + 2], 16) for i in (1, 3, 5)]
    w = percent / 100
    return "#" + "".join(f"{round(x * w + y * (1 - w)):02x}" for x, y in zip(a, b, strict=True))


def _pill_tint_percent() -> float:
    found = re.search(
        r"\.pill-g, \.pill-r, \.pill-a, \.pill-b \{[^}]*?"
        r"color-mix\(in srgb, var\(--pill-ink\) (\d+(?:\.\d+)?)%, var\(--card\)\)",
        CSS,
    )
    assert found, "the status pills' shared tint rule is gone"
    return float(found.group(1))


@pytest.mark.parametrize("theme", sorted(THEMES))
@pytest.mark.parametrize("sev", ["--sev-ok", "--sev-critical", "--sev-warning", "--sev-info"])
def test_a_status_pill_ink_clears_4_5_on_its_own_tint(theme: str, sev: str) -> None:
    # The pill's background is an opaque tint of the card, so this pair is what
    # the user sees wherever the pill sits (the workspace's "Draft" was 4.17:1).
    ink = _hex(theme, sev)
    tint = _mix(ink, _pill_tint_percent(), _hex(theme, "--card"))
    ratio = _ratio(ink, tint)
    assert ratio >= 4.5, f"{theme} {sev} on its {_pill_tint_percent()}% tint: {ratio:.2f}:1"


@pytest.mark.parametrize("theme", sorted(THEMES))
def test_placeholder_text_clears_4_5_on_the_second_surface_too(theme: str) -> None:
    # Every placeholder reads --t4 (global.css `::placeholder`); fields sit on
    # the card, the page, the surface and the second surface.
    ratio = _ratio(_hex(theme, "--t4"), _hex(theme, "--surface-2"))
    assert ratio >= 4.5, f"{theme} --t4 on --surface-2: {ratio:.2f}:1"


SPLIT_CSS = (
    pathlib.Path(__file__).resolve().parents[1] / "static/css/timetable-workspace-split.css"
).read_text(encoding="utf-8")


def _split(token: str) -> str:
    start = SPLIT_CSS.index('html[data-theme="dark"] body.tws-root {')
    block = SPLIT_CSS[start : SPLIT_CSS.index("\n}", start)]
    found = re.search(rf"{re.escape(token)}:\s*(#[0-9a-fA-F]{{6}})\b", block)
    assert found, f"split view's token block has no plain hex {token}"
    return found.group(1)


@pytest.mark.parametrize("surface", ["--card", "--bg"])
def test_split_views_dark_palette_gives_brand_text_its_dark_value(surface: str) -> None:
    # Split view's own dark palette (it follows the theme since style check S3)
    # must give the shared rules it renders in --text-brand (the English heading)
    # the dark value: the light value is 2.53:1 on its card; teal, before C1, was
    # 4.29:1 there.
    ratio = _ratio(_split("--text-brand"), _split(surface))
    assert ratio >= 4.5, f"split --text-brand on {surface}: {ratio:.2f}:1"
    light = _ratio(_hex("light", "--text-brand"), _split(surface))
    assert light < 4.5, "the light value passes here now: the split override may go"


# ── Stage 3, C1 after review: brand text on a teal wash ──────────────────────
# Chips, active rows and badges paint --text-brand on a translucent teal wash
# (--teal-dim, --brand-soft, --teal-border or a color-mix of --teal), and the
# wash darkens whatever it sits on: #067858 measured 4.31:1 on --teal-dim over
# --surface-2 and 4.25:1 on the planner's "Add selected". --brand-dark is the
# same text colour under its old name.

BRAND_TEXT = ("--text-brand", "--brand-dark")
TEAL_WASHES = ("--teal-dim", "--brand-soft", "--teal-border")
SURFACES = ("--card", "--bg", "--surface", "--surface-2")


def _rgba(theme: str, token: str) -> tuple[str, float]:
    found = re.search(
        rf"{re.escape(token)}:\s*rgba\((\d+),\s*(\d+),\s*(\d+),\s*([\d.]+)\)", THEMES[theme]
    )
    assert found, f"{token} is not an rgba() colour in the {theme} block"
    r, g, b, a = found.groups()
    return f"#{int(r):02x}{int(g):02x}{int(b):02x}", float(a)


def test_brand_dark_is_the_brand_text_colour_in_both_themes() -> None:
    # The lint lets --brand-dark be text because it IS --text-brand.
    for theme in THEMES:
        assert _hex(theme, "--brand-dark").lower() == _hex(theme, "--text-brand").lower(), theme


@pytest.mark.parametrize("theme", sorted(THEMES))
@pytest.mark.parametrize("token", BRAND_TEXT)
@pytest.mark.parametrize("wash", TEAL_WASHES)
def test_brand_text_clears_4_5_on_a_teal_wash_over_every_surface(
    theme: str, token: str, wash: str
) -> None:
    colour, alpha = _rgba(theme, wash)
    low = []
    for surface in SURFACES:
        ratio = _ratio(_hex(theme, token), _mix(colour, alpha * 100, _hex(theme, surface)))
        if ratio < 4.5:
            low.append(f"{surface} {ratio:.2f}:1")
    assert low == [], f"{theme} {token} on {wash} over: {', '.join(low)}"


_WASH = re.compile(
    r"var\(--(teal-dim|brand-soft|teal-border)\)"
    r"|color-mix\(in srgb,\s*var\(--(teal|teal-bright|brand|text-brand)\)\s+(\d+(?:\.\d+)?)%,"
    r"\s*(transparent|var\(--[\w-]+\))\)"
)


def _plain_hex(theme: str, token: str) -> str | None:
    found = re.search(rf"{re.escape(token)}:\s*(#[0-9a-fA-F]{{6}})\b", THEMES[theme])
    return found.group(1) if found else None


def _under(theme: str, background: str) -> list[tuple[str, str]]:
    """Each teal wash in a background value, as (where, the opaque colour it
    makes) over every surface it may sit on; an opaque mix, over its own base."""
    out = []
    for m in _WASH.finditer(background):
        if m.group(1):
            colour, alpha = _rgba(theme, "--" + m.group(1))
            bases = SURFACES
        else:
            colour = _hex(theme, "--" + m.group(2))
            alpha = float(m.group(3)) / 100
            bases = SURFACES if m.group(4) == "transparent" else (m.group(4)[4:-1],)
        for base in bases:
            under = _plain_hex(theme, base)
            if under:  # a base that is not a plain colour is not measured here
                out.append((f"{m.group(0)[:48]} over {base}", _mix(colour, alpha * 100, under)))
    return out


def test_every_brand_text_rule_on_a_teal_wash_clears_4_5() -> None:
    # Every stylesheet's rules (MRI's excepted, as in the lint),
    # each wash at its own strength: the strongest today is 16% (--teal-border).
    # A rule that repaints only the wash (a hover: `.pl-btn-teal:hover` on
    # --teal-border) is read with the brand ink a rule for its element gives it.
    from tests.test_design_contrast_lint import (
        CSS_DIR,
        EXCLUDED,
        _compounds,
        _covers,
        _declarations,
        _rules,
    )

    brand = tuple(f"var({t})" for t in BRAND_TEXT)
    rules = []
    for path in sorted(CSS_DIR.glob("*.css")):
        if path.name not in EXCLUDED:
            for _, selector, body, line in _rules(path.read_text(encoding="utf-8")):
                decls = dict(_declarations(body))
                ink = (decls.get("color") or "").replace("!important", "").strip()
                rules.append((path.name, selector, decls, ink, line))
    inks = [
        (_compounds(part), ink)
        for _, selector, _, ink, _ in rules
        if ink in brand
        for part in selector.split(",")
    ]

    measured, by_cascade, low = 0, 0, []
    for name, selector, decls, ink, line in rules:
        background = " ".join(decls.get(p, "") for p in ("background", "background-color"))
        if ink in brand:
            parts = [(part, ink) for part in selector.split(",")]
        elif not ink and _WASH.search(background):
            parts = [
                (part, next(i for c, i in inks if _covers(c, _compounds(part))))
                for part in selector.split(",")
                if any(_covers(c, _compounds(part)) for c, _ in inks)
            ]
            by_cascade += len(parts)
        else:
            continue
        for part, part_ink in parts:
            dark_only = 'data-theme="dark"' in part
            for theme in ("dark",) if dark_only else sorted(THEMES):
                for where, under in _under(theme, background):
                    measured += 1
                    ratio = _ratio(_hex(theme, part_ink[4:-1]), under)
                    if ratio < 4.5:
                        low.append(f"{name}:{line} {part.strip()} {theme} {where} {ratio:.2f}:1")
    assert measured > 100, "the sweep found almost no brand text on a wash"
    assert by_cascade > 0, "the sweep read no wash through the cascade"
    assert low == [], "\n".join(low)

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
    start = SPLIT_CSS.index("html body.tws-root {")
    block = SPLIT_CSS[start : SPLIT_CSS.index("\n}", start)]
    found = re.search(rf"{re.escape(token)}:\s*(#[0-9a-fA-F]{{6}})\b", block)
    assert found, f"split view's token block has no plain hex {token}"
    return found.group(1)


@pytest.mark.parametrize("surface", ["--card", "--bg"])
def test_split_views_dark_palette_gives_brand_text_its_dark_value(surface: str) -> None:
    # Split view is dark in both themes, so the shared rules it renders in
    # --text-brand (the English heading) must not take the light value, which
    # is 3.23:1 on its card; teal, before C1, was 4.29:1 there.
    ratio = _ratio(_split("--text-brand"), _split(surface))
    assert ratio >= 4.5, f"split --text-brand on {surface}: {ratio:.2f}:1"
    light = _ratio(_hex("light", "--text-brand"), _split(surface))
    assert light < 4.5, "the light value passes here now: the split override may go"

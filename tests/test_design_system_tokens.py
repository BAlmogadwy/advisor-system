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

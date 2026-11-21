"""One type scale, one control ladder, and a ratchet so the sprawl cannot regrow.

The premium UI review (2026-11) measured 759 font-size declarations with 115
distinct values in global.css alone (167 distinct sizes rendered across 403
Chrome captures), weights 650/750/850 that no loaded face has, and literal
radii from 2px to 24px beside the radius tokens. On a phone the fluid 14px root
shrank every raw rem size under its own design size: body copy rendered at
11.4px and labels at 9.8-10.9px. A codemod moved the literals onto the tokens;
these tests keep them there:

- every step of the type scale carries a px floor, so no phone renders it smaller;
- the control ladder exists, and touch redefines it once (no per-component
  media-query heights);
- raw font sizes, off-scale weights and literal radii may only become fewer;
- the global resets added with it never reach the MRI page, the classic
  inspector's MRI tab or split view (owner: out of scope), nor the classic
  Timetable Workspace's measured board geometry.
"""

from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
CSS_DIR = ROOT / "static/css"
GLOBAL = (CSS_DIR / "global.css").read_text(encoding="utf-8")

# The sheets the scale governs. Not the MRI sheets or split view's (owner: out
# of scope), and not timetable-meeting-policy.css, which split view also loads.
SHEETS = (
    "global.css",
    "bootstrap-compat.css",
    "page-exam-timetable.css",
    "page-exam-rosters.css",
    "page-sections-import.css",
    "group-availability.css",
    "timetable-graph.css",
    "page-student-feedback.css",
    "page-staff-feedback.css",
    "timetable-workspace.css",
)
# Rules for the MRI page, the classic MRI tab and split view, and the sidebar
# (the owner's own design, sized against measured label fits) are not counted.
EXEMPT = re.compile(
    r"\.(twm-|tw-mri|tws-)|\.logo-|\.sidebar|#appSidebar|"
    r"\.nav-(a|link|lb|lbl|grp|sec|find|section|tip)|\.s-usr|\.s-foot|\.s-lang|"
    r"\.sb-|\.sbp-|\.theme-toggle|\.u-rl|\.u-nm|\.avatar"
)


def _root_block() -> str:
    start = GLOBAL.index("\n:root {")
    return GLOBAL[start : GLOBAL.index("\n}", start)]


def _declarations(css: str):
    """(selector, at-rules, property, value) for every declaration in a sheet."""
    css = re.sub(r"/\*.*?\*/", " ", css, flags=re.S)
    stack: list[str] = []
    buf = ""
    for ch in css:
        if ch == "{":
            stack.append(" ".join(buf.split()))
            buf = ""
        elif ch in ";}":
            decl = buf.strip()
            buf = ""
            selectors = [s for s in stack if not s.startswith("@")]
            if selectors and ":" in decl and not decl.startswith("--"):
                prop, value = decl.split(":", 1)
                yield (
                    selectors[-1],
                    [s for s in stack if s.startswith("@")],
                    prop.strip(),
                    value.strip(),
                )
            if ch == "}" and stack:
                stack.pop()
        else:
            buf += ch


def _counted():
    for name in SHEETS:
        for selector, at_rules, prop, value in _declarations(
            (CSS_DIR / name).read_text(encoding="utf-8")
        ):
            if EXEMPT.search(selector) or any("print" in a for a in at_rules):
                continue
            yield name, selector, prop, value.replace("!important", "").strip()


def _top_level(selector_list: str) -> list[str]:
    """Split a selector list on its own commas, not those inside :where(…)."""
    parts, depth, current = [], 0, ""
    for ch in selector_list:
        depth += {"(": 1, ")": -1}.get(ch, 0)
        if ch == "," and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += ch
    parts.append(current)
    return [p.strip() for p in parts if p.strip()]


def _raw_size(value: str) -> bool:
    """A literal rem/px size (or a clamp/max of them) rather than a token."""
    if "var(--text-" in value or "var(--control-fs" in value or "var(--field-fs" in value:
        return False
    return bool(re.search(r"\d(\.\d+)?(rem|px)\b", value))


def test_every_step_of_the_type_scale_has_a_px_floor() -> None:
    root = _root_block()
    floors = {
        "--text-micro": 11,
        "--text-xs": 12,
        "--text-sm": 13,
        "--text-base": 14,
        "--text-md": 16,
        "--text-lg": 18,
        "--text-xl": 20,
        "--text-display": 24,
        "--text-numeral": 28,
    }
    for token, px in floors.items():
        value = re.search(rf"{re.escape(token)}:\s*([^;]+);", root)
        assert value, f"{token} is missing from the first :root block"
        assert value.group(1).replace(" ", "").startswith(f"max({px}px,"), (
            f"{token} must be max({px}px, …rem): a bare rem renders under its own "
            f"size on a phone's 14px root (it is {value.group(1)!r})"
        )


def test_the_control_ladder_exists_and_touch_redefines_it_once() -> None:
    root = _root_block()
    for token, value in {
        "--control-h-sm": "32px",
        "--control-h": "38px",
        "--control-h-lg": "44px",
        "--control-fs": "var(--text-base)",
        "--field-fs": "var(--text-base)",
    }.items():
        assert re.search(rf"{re.escape(token)}:\s*{re.escape(value)};", root), token
    touch = re.search(
        r"@media \(max-width: 768px\), \(pointer: coarse\) \{\s*:root \{([^}]*)\}", GLOBAL
    )
    assert touch, "touch devices redefine the control ladder in one :root block"
    assert "--control-h:    44px" in touch.group(1)
    assert "--field-fs:     var(--text-md)" in touch.group(1), "16px field text: no iOS zoom"
    # The buttons and fields read the ladder; no per-component phone heights.
    assert re.search(r"html body \.btn \{[^}]*min-height:\s*var\(--control-h\);", GLOBAL)
    assert re.search(r"\.form-select \{[^}]*min-height:\s*var\(--control-h\);", GLOBAL)
    assert "html body .btn { min-height: 34px" not in GLOBAL


def test_raw_font_sizes_only_ever_get_fewer() -> None:
    raw = [
        f"{n}: {s} {{font-size: {v}}}"
        for n, s, p, v in _counted()
        if p == "font-size" and _raw_size(v)
    ]
    # Display numerals, hero headings and glyph sizes stay fluid on purpose.
    assert len(raw) <= 20, "a new raw font-size: use a --text-* token\n" + "\n".join(raw)


def test_no_off_scale_font_weights() -> None:
    allowed = {"400", "500", "600", "700", "normal", "bold", "inherit", "bolder", "lighter"}
    found = [
        f"{n}: {s} {{font-weight: {v}}}"
        for n, s, p, v in _counted()
        if p == "font-weight" and v not in allowed
    ]
    # The shorthand too: `font: 750 .88rem/1.2 …` slipped past the weight sweep once.
    found += [
        f"{n}: {s} {{font: {v}}}"
        for n, s, p, v in _counted()
        if p == "font"
        and re.match(r"(italic\s+)?(\d{3})\b", v)
        and re.match(r"(italic\s+)?(\d{3})\b", v).group(2) not in allowed
    ]
    assert found == [], "weights are 400/500/600/700 (Inter has no 650/750/850):\n" + "\n".join(
        found
    )


def test_literal_radii_only_ever_get_fewer() -> None:
    literal = [
        f"{n}: {s} {{{p}: {v}}}"
        for n, s, p, v in _counted()
        if p.endswith("radius")
        and "var(" not in v
        and "calc(" not in v
        and any(float(x) > 3.5 for x, _ in re.findall(r"(\d+(?:\.\d+)?)(px|rem)", v))
    ]
    assert len(literal) == 0, "a new literal radius: use a --radius-* token\n" + "\n".join(literal)


def test_the_global_resets_never_reach_mri_split_or_the_workspace_board() -> None:
    reset = re.search(
        r"([^{}]*)\{\s*box-sizing: border-box;\s*\}", GLOBAL[GLOBAL.index("/* Box model:") :]
    )
    assert reset, "the box-sizing reset is where its comment says"
    selectors = _top_level(re.sub(r"/\*.*?\*/", "", reset.group(1), flags=re.S))
    assert len(selectors) == 3, f"the element, ::before and ::after: {selectors}"
    for selector in selectors:  # every one of them, not just the first
        for excluded in (
            ":not(.twm-root)",
            ":not(.tws-root)",
            ":not(:has(#twWorkspace))",
            ".tw-mri *",
        ):
            assert excluded in selector, f"{selector!r} must exclude {excluded}"
        assert selector.count(":where(") >= 2, (
            "zero specificity: any component's own box-sizing wins"
        )
    # The control font, link, checkbox and strong defaults are zero-specificity
    # and skip the same pages.
    for marker in (
        ":where(input, select, textarea)",
        ":where(a:not([class]))",
        ':where(input[type="checkbox"], input[type="radio"])',
        ":where(b, strong)",
    ):
        line = next(text for text in GLOBAL.splitlines() if marker in text)
        assert ":where(body:not(.twm-root):not(.tws-root))" in line, line
        assert ".tw-mri *" in line, line
    # MRI's body keeps the old fluid clamp: the 15px body is scoped away from it.
    assert "body:not(.twm-root):not(.tws-root) { font-size: max(14px, 0.9375rem); }" in GLOBAL

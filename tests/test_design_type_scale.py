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
    r"\.sb-|\.sbp\b|\.sbp-|\.theme-toggle|\.u-rl|\.u-nm|\.avatar"
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
            # Exempt only when EVERY selector in the list is: a list that mixes a
            # sidebar selector with an app one is still counted.
            exempt = all(EXEMPT.search(s) for s in _top_level(selector))
            if exempt or any("print" in a for a in at_rules):
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
        "--text-body": 14,
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
    assert len(raw) <= 19, "a new raw font-size: use a --text-* token\n" + "\n".join(raw)


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
        # in px: a 0.5rem radius is 8px, not 0.5
        and any(
            float(x) * (16 if unit == "rem" else 1) > 3.5
            for x, unit in re.findall(r"(\d+(?:\.\d+)?)(px|rem)", v)
        )
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
            # the sidebar, its palette and its phone menu button: owner design
            ".sidebar *",
            ".sbp *",
            ".sidebar-toggle *",
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
    # The control-font defaults also leave the sidebar and its palette alone.
    for marker in (":where(input, select, textarea)", ":where(button)"):
        line = next(text for text in GLOBAL.splitlines() if marker in text and "font-size" in text)
        assert ".sidebar *" in line and ".sbp *" in line, line
    # MRI's body keeps the old fluid clamp: the 15px body is scoped away from it,
    # and the sidebar and its palette keep the size they were measured at.
    assert "body:not(.twm-root):not(.tws-root) { font-size: var(--text-body); }" in GLOBAL
    assert ".sidebar, .sbp { font-size: clamp(0.8125rem, 0.742rem + 0.19vw, 0.9375rem); }" in GLOBAL


def _rule(selector: str) -> str:
    """The declarations of the first rule written as `<selector> {`."""
    at = GLOBAL.index(selector + " {")
    return GLOBAL[at : GLOBAL.index("}", at)]


def _tokens(declarations: str) -> dict[str, str]:
    return dict(re.findall(r"(--[\w-]+):\s*([^;]+);", declarations))


ARABIC = 'html[dir="rtl"]:not(:has(> body.twm-root, > body.tws-root))'
HELD = 'html[dir="rtl"] :where(.sidebar, .sbp, .sidebar-toggle, .sb-tip,'
TOUCH = "@media (max-width: 768px), (pointer: coarse) {\n  "


def test_the_root_is_the_fluid_clamp_and_not_a_step_of_the_scale() -> None:
    """html's own size is what every rem multiplies: 14px at 375px wide to 16px
    at 1440px. The premium review's codemod (#250) rewrote the clamp as
    var(--text-md), which fixed the root at 16px and moved the MRI page, the
    classic MRI tab and split view (owner: unchanged) at every width under 1440px
    -- and an Arabic --text-md would have grown every rem on the page."""
    assert re.search(
        r"--text-root:\s*clamp\(0\.875rem, 0\.831rem \+ 0\.19vw, 1rem\);", _root_block()
    )
    html = re.search(r"\nhtml \{([^}]*)\}", GLOBAL)
    assert html, "the html rule"
    assert re.search(r"font-size:\s*var\(--text-root\);", html.group(1)), html.group(1)
    assert GLOBAL.count("--text-root:") == 1, "no language or page redefines the root"


def test_arabic_has_its_own_steps_on_every_page_but_mri_and_split() -> None:
    """Noto Kufi's letter body is 48-50px at 100px against Inter's 55px x-height,
    so 12px Arabic read like 10.4px Latin. The Arabic steps are about 15% larger.
    They are set on <html>, the element the aliases are computed on, and kept
    off the MRI page and split view by those pages' body class."""
    tokens = _tokens(_rule(ARABIC))
    floors = {
        "--fs-floor": "13px",
        "--text-micro": "max(13px,",
        "--text-xs": "max(14px,",
        "--text-sm": "max(15px,",
        "--text-base": "max(16px,",
        "--text-md": "max(18px,",
        "--text-lg": "max(20px,",
        "--text-body": "max(16px,",
        # controls stop a step short: filter bars keep to one line
        "--control-fs-sm": "max(14px,",
        "--control-fs": "max(15px,",
        "--field-fs-sm": "max(14px,",
        "--field-fs": "max(15px,",
    }
    for token, start in floors.items():
        assert tokens.get(token, "").startswith(start), (token, tokens.get(token))
    # Latin digits (the KPI numerals) and the serif masthead, whose x-height
    # equals the Kufi letter body, are not scaled; nor is the root.
    for token in ("--text-xl", "--text-display", "--text-numeral", "--text-root"):
        assert token not in tokens, token
    # A phone or tablet keeps 16px field text (no iOS zoom): the :root touch
    # block is less specific than the Arabic one, so it is restated.
    touch = _tokens(GLOBAL[GLOBAL.index(TOUCH + ARABIC) :].split("}", 1)[0])
    assert touch == {"--field-fs": "max(16px, 1rem)", "--field-fs-sm": "max(16px, 1rem)"}, touch


def test_the_held_zones_keep_todays_arabic_sizes() -> None:
    """The owner's sidebar (fitted to its 252/210/195px rail), the workspace's
    chrome (a 170px budget, already 170px in Arabic) and board grid, and the
    exam builder's grid, toolbar and fixed-height chips keep their sizes. A
    token computed on <html> arrives as a finished value, so every one the
    Arabic scale moves is restated, at the first :root block's value."""
    held_at = GLOBAL.index(HELD)
    selectors = GLOBAL[held_at : GLOBAL.index("{", held_at)]
    for zone in (
        ".sidebar", ".sbp", ".sidebar-toggle", ".sb-tip",
        ".tw-header", ".tw-cmd-bar", ".tw-stats", ".tw-panel-tabs", ".tw-grid-table", "#twWorkspace",
        "#schedGrid", ".et-edit-controls", "#progList", "#secList",
        ".audit-table", ".tg-shell",
    ):  # fmt: skip
        assert zone in selectors, zone
    held = _tokens(GLOBAL[held_at : GLOBAL.index("}", held_at)])
    assert set(_tokens(_rule(ARABIC))) <= set(held), "every Arabic step is restated"
    root = _tokens(_root_block())
    for token in ("--text-xs", "--text-sm", "--text-base", "--text-md", "--text-lg", "--text-body"):
        assert held[token] == root[token], (token, held[token], root[token])
    assert (held["--text-micro"], held["--fs-floor"]) == ("max(12px, 0.75rem)", "12px")
    for alias, step in (
        ("--text-2xs", "--text-micro"),
        ("--fs-sm", "--text-micro"),
        ("--control-fs-sm", "--text-sm"),
        ("--control-fs", "--text-base"),
        ("--field-fs-sm", "--text-sm"),
        ("--field-fs", "--text-base"),
    ):
        assert held[alias] == f"var({step})" == root[alias], alias
    touch = _tokens(GLOBAL[GLOBAL.index(TOUCH + HELD) :].split("}", 1)[0])
    assert touch == {"--field-fs": "var(--text-md)", "--field-fs-sm": "var(--text-md)"}, touch


def test_the_arabic_type_rules_never_reach_mri_split_or_the_sidebar() -> None:
    """The face, weight and leading rules for Arabic labels, headings and
    placeholders use broad patterns ([class*="kicker"], .badge, input); each
    skips the MRI page and split view by body class, and the classic MRI tab
    and the sidebar by ancestry."""
    rules = [
        line
        for line in GLOBAL.split("}")
        if re.search(
            r'html\[dir="rtl"\] body:not\(\.twm-root\):not\(\.tws-root\) (:is|\.badge)', line
        )
    ]
    # the face, the weight cap, the badge and heading leading, the placeholder face
    assert len(rules) >= 5, len(rules)
    for rule in rules:
        selector = re.sub(r"/\*.*?\*/", "", rule, flags=re.S).split("{")[0]
        for skipped in (".tw-mri *", ".sidebar *", ".sbp *"):
            assert skipped in selector, (skipped, selector.strip()[:120])

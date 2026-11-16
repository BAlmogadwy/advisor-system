"""CI enforcement for the CSS collision gate, and mutation tests for it.

The pre-commit hook only runs where pre-commit is installed, so the gate
itself runs here too, over every tracked stylesheet.

The rest of this file exists because a passing checker proves nothing.  Each
test below breaks something and asserts the checker goes red -- including the
three real collisions this gate was written for, reconstructed from the
November 2026 stylesheet.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "check_css_collisions.py"

sys.path.insert(0, str(REPO))
from scripts.check_css_collisions import collisions_in  # noqa: E402


def props_flagged(src: str) -> set:
    return {(sel, prop) for _ctx, sel, prop, _dead, _win in collisions_in(src)}


def test_tracked_stylesheets_introduce_no_new_collisions():
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--all-tracked"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_baseline_has_no_stale_entries():
    """The baseline may only shrink.  A stale entry means something was fixed
    and the record was not tightened -- harmless, but it hides the next bug on
    that selector and property."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--all-tracked"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert "now fixed" not in result.stderr, result.stderr


# --- mutation tests: each of these MUST be detected -----------------------


def test_detects_a_freshly_injected_collision():
    src = """
    .widget { color: red; padding: 4px; }
    .widget { color: blue; }
    """
    assert (".widget", "color") in props_flagged(src)
    assert (".widget", "padding") not in props_flagged(src)


def test_detects_the_kpi_collision():
    """`.kpi` was defined in a group rule and again standalone."""
    src = """
    .kpi, .metric { background: var(--card); border: 1px solid var(--line); }
    .kpi { background: none; border: none; }
    """
    flagged = props_flagged(src)
    assert (".kpi", "background") in flagged
    assert (".kpi", "border") in flagged
    assert (".metric", "background") not in flagged


def test_detects_the_dark_batch_metrics_collision():
    """Declared twice under the same dark-theme prefix."""
    src = """
    html[data-theme="dark"] #batchMetrics .metric { background: #111; }
    html[data-theme="dark"] #batchMetrics .metric { background: #222; }
    """
    assert ('html[data-theme="dark"] #batchMetrics .metric', "background") in (props_flagged(src))


def test_detects_a_collision_across_thousands_of_lines():
    """`.audit-table-wrap` collided from 5524 to 9327; distance must not
    matter."""
    src = (
        ".audit-table-wrap { overflow: auto; background: var(--card); }\n"
        + "/* filler */\n" * 4000
        + ".audit-table-wrap { overflow: hidden; background: none; }\n"
    )
    flagged = props_flagged(src)
    assert (".audit-table-wrap", "overflow") in flagged
    assert (".audit-table-wrap", "background") in flagged


def test_reports_the_dead_line_not_the_winning_one():
    src = ".a { color: red; }\n.b { color: red; }\n.a { color: blue; }\n"
    hits = [h for h in collisions_in(src) if h[1] == ".a"]
    assert len(hits) == 1
    _ctx, _sel, prop, dead_line, win_line = hits[0]
    assert (prop, dead_line, win_line) == ("color", 1, 3)


# --- and these must NOT be flagged ----------------------------------------


def test_same_selector_different_properties_is_not_a_collision():
    src = ":root { --a: 1px; }\n:root { --b: 2px; }\n"
    assert collisions_in(src) == []


def test_media_query_context_is_not_mixed_with_the_base_rule():
    src = """
    .box { width: 100%; }
    @media (max-width: 700px) { .box { width: 50%; } }
    """
    assert collisions_in(src) == []


def test_distinct_keyframes_sharing_step_names_is_not_a_collision():
    src = """
    @keyframes spin { to { transform: rotate(360deg); } }
    @keyframes drift { to { transform: translateY(4px); } }
    """
    assert collisions_in(src) == []


def test_a_comma_list_repeating_a_selector_is_one_rule():
    src = ".a, .a { color: red; }\n"
    assert collisions_in(src) == []


def test_an_earlier_important_declaration_still_applies():
    src = ".a { color: red !important; }\n.a { color: blue; }\n"
    assert collisions_in(src) == []


def test_a_later_important_declaration_kills_the_earlier_one():
    src = ".a { color: red; }\n.a { color: blue !important; }\n"
    assert (".a", "color") in props_flagged(src)


def test_a_fallback_stack_inside_one_rule_is_not_a_collision():
    src = ".a { color: #0a8; color: color-mix(in srgb, var(--teal) 50%, white); }\n"
    assert collisions_in(src) == []


def test_declarations_in_a_comment_are_ignored():
    src = ".a { color: red; }\n/* .a { color: blue; } */\n"
    assert collisions_in(src) == []


def test_a_semicolon_inside_a_string_does_not_split_a_declaration():
    src = ".a::after { content: 'a;b'; color: red; }\n.a::after { color: blue; }\n"
    flagged = props_flagged(src)
    assert (".a::after", "color") in flagged
    assert (".a::after", "content") not in flagged


def test_the_mri_stylesheets_are_out_of_scope():
    """The owner has ruled the MRI pages off-limits; this gate must never be
    the reason one of them has to change."""
    from scripts.check_css_collisions import css_files

    kept = css_files(
        [
            "static/css/global.css",
            "static/css/timetable-mri.css",
            "static/css/timetable-mri-full.css",
        ],
        all_tracked=False,
    )
    assert kept == ["static/css/global.css"]


def test_baseline_is_valid_json_and_sorted():
    data = json.loads(
        (REPO / "scripts" / "css_collision_baseline.json").read_text(encoding="utf-8")
    )
    entries = data["entries"]
    assert entries == sorted(entries)
    assert len(entries) == len(set(entries))


def test_the_cross_selector_case_is_knowingly_out_of_scope():
    """The sibling defect -- two DIFFERENT selectors overlapping, the later or
    more specific one winning -- is NOT detected, and that is deliberate: a
    detector for it reported 15,399 pairs on this stylesheet, essentially all
    of them intentional overrides.

    The positive control in the same test is what stops this being vacuous:
    change ONE character so the selectors match, and the checker must fire.
    """
    overlapping = (
        ".table-wrap thead th { position: sticky; }\n"
        ".table-wrap thead th[data-sort] { position: relative; }\n"
    )
    identical = (
        ".table-wrap thead th { position: sticky; }\n.table-wrap thead th { position: relative; }\n"
    )
    assert collisions_in(overlapping) == []
    assert (".table-wrap thead th", "position") in props_flagged(identical)


# --- selector lists: commas that are NOT separators ------------------------


@pytest.mark.parametrize(
    "first,second",
    [
        (".x:is(.a, .b) .c", ".y:is(.z, .b) .c"),
        (":not(.a, .b)", ":not(.c, .b)"),
        ('[data-x="a,b"]', '[data-x="c,b"]'),
        ("main :is(.et-grid, .table) :is(th, td)", "#grid .et-grid :is(th, td)"),
        (".a:has(> b, c)", ".d:has(> e, c)"),
    ],
)
def test_a_comma_inside_a_selector_is_not_a_separator(first, second):
    """Splitting on every comma produced baseline keys like `td)` and
    `.btn-success)`, which both masked real collisions and manufactured
    false ones between unrelated rules."""
    src = f"{first} {{ color: red; }}\n{second} {{ color: blue; }}\n"
    assert collisions_in(src) == []


def test_a_real_selector_list_still_splits():
    src = ".a, .b { color: red; }\n.b { color: blue; }\n"
    flagged = props_flagged(src)
    assert (".b", "color") in flagged
    assert (".a", "color") not in flagged


def test_the_is_selector_still_collides_with_itself():
    src = (
        "main :is(.et-grid, .table) :is(th, td) { padding: 1px; }\n"
        "main :is(.et-grid, .table) :is(th, td) { padding: 2px; }\n"
    )
    assert ("main :is(.et-grid, .table) :is(th, td)", "padding") in props_flagged(src)


def test_whitespace_inside_an_attribute_value_is_significant():
    src = '[a="x > y"] { color: red; }\n[a="x>y"] { color: blue; }\n'
    assert collisions_in(src) == []


# --- !important and custom properties --------------------------------------


def test_important_spelled_with_a_space_still_protects_the_earlier_rule():
    src = ".a { color: red ! important; }\n.a { color: blue; }\n"
    assert collisions_in(src) == []


def test_important_inside_a_string_is_not_an_importance_flag():
    src = ".a::after { content: '!important'; }\n.a::after { content: 'x'; }\n"
    assert (".a::after", "content") in props_flagged(src)


def test_custom_property_names_are_case_sensitive():
    src = ":root { --Foo: 1px; }\n:root { --foo: 2px; }\n"
    assert collisions_in(src) == []


def test_a_custom_property_still_collides_with_its_exact_self():
    src = ":root { --foo: 1px; }\n:root { --foo: 2px; }\n"
    assert (":root", "--foo") in props_flagged(src)


def test_two_rules_sharing_one_physical_line_are_still_compared():
    src = ".a { color: red; } .a { color: blue; }\n"
    assert (".a", "color") in props_flagged(src)


# --- the baseline may only shrink ------------------------------------------


def _baseline_run(tmp_path, monkeypatch, css, entries, *args):
    import scripts.check_css_collisions as mod

    sheet = tmp_path / "static" / "css" / "probe.css"
    sheet.parent.mkdir(parents=True)
    sheet.write_text(css, encoding="utf-8")
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({"entries": entries}), encoding="utf-8")
    monkeypatch.setattr(mod, "REPO", tmp_path)
    monkeypatch.setattr(mod, "BASELINE_PATH", baseline)
    code = mod.main(["static/css/probe.css", *args])
    return code, json.loads(baseline.read_text(encoding="utf-8"))["entries"]


def test_update_baseline_refuses_to_adopt_a_new_collision(tmp_path, monkeypatch):
    code, entries = _baseline_run(
        tmp_path,
        monkeypatch,
        ".a { color: red; }\n.a { color: blue; }\n",
        [],
        "--update-baseline",
    )
    assert code == 1
    assert entries == []


def test_update_baseline_prunes_what_was_fixed(tmp_path, monkeypatch):
    key = "static/css/probe.css||.a|color"
    code, entries = _baseline_run(
        tmp_path,
        monkeypatch,
        ".a { color: red; }\n",
        [key, "static/css/other.css||.b|color"],
        "--update-baseline",
    )
    assert code == 0
    # the fixed entry is gone; the UNSCANNED file's entry survives
    assert entries == ["static/css/other.css||.b|color"]


def test_a_partial_scan_does_not_wipe_other_files(tmp_path, monkeypatch):
    others = [f"static/css/other{i}.css||.x{i}|color" for i in range(5)]
    _code, entries = _baseline_run(
        tmp_path, monkeypatch, ".a { color: red; }\n", others, "--update-baseline"
    )
    assert sorted(entries) == sorted(others)


def test_a_css_file_outside_the_repo_does_not_crash(tmp_path, monkeypatch):
    import scripts.check_css_collisions as mod

    outside = tmp_path / "outside.css"
    outside.write_text(".a { color: red; }\n.a { color: blue; }\n", encoding="utf-8")
    monkeypatch.setattr(mod, "REPO", tmp_path / "repo")
    (tmp_path / "repo").mkdir()
    assert mod.main([str(outside)]) == 0

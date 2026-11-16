"""Where two i18n mechanisms translate one string, they must agree.

The staff dashboard runs two of this project's four translation mechanisms at
the same time:

* `core/templates/core/partials/dashboard/_js_i18n.html` keeps a `MAP` of
  English -> Arabic and walks the DOM replacing leaf text, which covers every
  label the SERVER rendered;
* `static/js/page-dashboard.js` keeps its own `T = { key: IS_AR ? ar : en }`
  table for every string the page GENERATES, and writes several of the same
  cells by `innerHTML` or `textContent`.

So one English string can be translated by both, and the user sees whichever
ran last. When the two give different Arabic, a caption rewords itself part way
through a session -- on the first status poll, or when a Reset button is
clicked -- with nothing failing anywhere. Code review caught one case
(`Run a batch preview to see top course recommendations.`), which is one more
than reading either file on its own would have caught.

This is a baselined gate, like `check_css_collisions.py`: the drift that
already exists is frozen below, and the test fails on a NEW divergence or on a
baselined one being resolved, so the list can only shrink deliberately.
"""

from __future__ import annotations

import re

from django.conf import settings

# Every English string both tables carry with DIFFERENT Arabic, as of
# 2026-11-16. None of these is known to show the reword: each is either a MAP
# key the walker cannot reach (the element has element children, or the string
# only ever exists because JS built it), or a label whose template renders its
# own Arabic through the `{% if LANGUAGE_CODE == 'ar' %}` idiom, so the MAP
# never decides it. They are frozen rather than fixed because choosing the
# surviving wording is a translation decision, not a bug fix, and the Arabic
# reviewer question is still open.
KNOWN_DIVERGENT = frozenset(
    {
        # Module blurbs: the dashboard's card subtitles. The JS rewrites them
        # when it re-renders a module card; both readings are accurate.
        "Click any course row to inspect lock reason, missing prereqs, and unlock impact.",
        "Create advisors and manage student-to-advisor assignments.",
        "Explore prerequisite links by program and optionally by a specific course.",
        "Get a per-student recommendation list and export it instantly.",
        "Inspect full program requirements, term breakdown, and credit totals.",
        "Quick links for all operational exports in one place.",
        "Run cohort-level preview, inspect top courses, and export aggregate CSV.",
        # Status words rendered inside a pill with a dot `<span>`, so the
        # walker skips the element and only the JS wording is ever seen.
        "Can register",
        "Idle",
        "Locked",
        "Passed",
        "Studying",
        # Built by JS as an `<option>` or a button label; the MAP key is dead.
        "Select advisor",
        "Apply CSV mapping",
        "Assign one student",
        "Enable students.advisor_id",
        # The template renders these through the idiom, so the walker never
        # decides them and only the JS wording can replace them.
        "No course selected.",
        "No logs yet.",
        "No prerequisite data loaded.",
    }
)

MAP_ENTRY = re.compile(r'^\s*"((?:[^"\\]|\\.)*)"\s*:\s*"((?:[^"\\]|\\.)*)"', re.M)
JS_ENTRY = re.compile(
    r"^\s*([A-Za-z_]\w*)\s*:\s*IS_AR\s*\?\s*"
    r"'((?:[^'\\]|\\.)*)'\s*:\s*'((?:[^'\\]|\\.)*)'",
    re.M,
)


def _map_table() -> dict[str, str]:
    source = (
        settings.BASE_DIR
        / "core"
        / "templates"
        / "core"
        / "partials"
        / "dashboard"
        / "_js_i18n.html"
    ).read_text(encoding="utf-8")
    body = source[source.index("const MAP = {") :]
    body = body[: body.index("\n    };")]
    return dict(MAP_ENTRY.findall(body))


def _js_table() -> dict[str, list[tuple[str, str]]]:
    """English -> [(T key, Arabic)], since two keys may share an English."""
    source = (settings.BASE_DIR / "static" / "js" / "page-dashboard.js").read_text(encoding="utf-8")
    out: dict[str, list[tuple[str, str]]] = {}
    for name, arabic, english in JS_ENTRY.findall(source):
        out.setdefault(english, []).append((name, arabic))
    return out


def _divergences() -> dict[str, list[tuple[str, str, str]]]:
    """English -> [(T key, MAP Arabic, JS Arabic)] for every disagreement."""
    mapping, js = _map_table(), _js_table()
    found: dict[str, list[tuple[str, str, str]]] = {}
    for english, arabic in mapping.items():
        for name, js_arabic in js.get(english, ()):
            if js_arabic != arabic:
                found.setdefault(english, []).append((name, arabic, js_arabic))
    return found


def test_both_tables_were_actually_parsed() -> None:
    """A regex that silently matches nothing would make this gate vacuous."""
    mapping, js = _map_table(), _js_table()
    assert len(mapping) > 250, f"only {len(mapping)} MAP entries parsed"
    assert len(js) > 150, f"only {len(js)} page-dashboard.js entries parsed"
    overlap = set(mapping) & set(js)
    assert len(overlap) > 25, (
        f"only {len(overlap)} English strings found in both tables; the two "
        "tables really do overlap, so this means a parser stopped working"
    )


def test_no_new_wording_divergence_between_the_two_tables() -> None:
    new = sorted(set(_divergences()) - KNOWN_DIVERGENT)
    found = _divergences()
    assert new == [], (
        "these strings are translated by BOTH the dashboard MAP and "
        "page-dashboard.js, with different Arabic, so whichever runs last "
        "wins and the label rewords itself mid-session. Match the wording, "
        "or add it to KNOWN_DIVERGENT with the reason it cannot show:\n"
        + "\n".join(
            f"  {english!r}\n      MAP:        {rows[0][1]}\n      js.{rows[0][0]}: {rows[0][2]}"
            for english, rows in ((e, found[e]) for e in new)
        )
    )


def test_the_baseline_has_no_stale_entries() -> None:
    """A resolved divergence must leave the list, or it stops meaning anything."""
    resolved = sorted(KNOWN_DIVERGENT - set(_divergences()))
    assert resolved == [], (
        "these no longer diverge -- remove them from KNOWN_DIVERGENT:\n"
        + "\n".join(f"  {english!r}" for english in resolved)
    )

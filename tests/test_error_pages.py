"""The 404 and 500 pages wear the app's design system without loading it.

`core/templates/404.html` and `500.html` are standalone documents on purpose.
Django renders the 500 through `django.views.defaults.server_error` with no
context and no request, and it has to render when what broke is something
base.html depends on: production's manifest storage makes `{% static %}` raise
when the manifest is missing or stale, a context processor can fail, the
database can be down. So neither page extends a template or links global.css;
each carries the design tokens it reads, copied from static/css/global.css.

This file holds that copy to the original, scope for scope (light, Deep Navy,
Arabic, the touch sizes): a token global.css changes fails here until the pages
follow. It holds the two pages to one stylesheet and one set of scripts, and it
checks what they were restyled for:
- the look is the system's: the paper ground and white card, the masthead's
  serif headline, Inter, the type scale, the control ladder in the primary pair;
- every text colour they paint clears 4.5:1, light and dark;
- the saved theme is applied the way base.html's anti-flash script applies it;
- the language is the one the app would render (the advisor_language cookie,
  then Accept-Language), with dir="rtl" for Arabic, through Django's handlers.
"""

from __future__ import annotations

import functools
import pathlib
import re

import pytest
from django.conf import settings as django_settings
from django.template import loader
from django.test import Client
from django.urls import path
from django.utils import translation

from tests.test_design_contrast_lint import _declarations, _rules

ROOT = pathlib.Path(__file__).resolve().parents[1]
PAGES = ("404.html", "500.html")

#: The selectors a design token is themed under, in global.css and on the pages.
LIGHT = ((), ":root")
DARK = ((), 'html[data-theme="dark"]')
THEMED = (":root", 'html[data-theme="dark"]', 'html[data-theme="light"]', 'html[dir="rtl"]')


def _source(name: str) -> str:
    return (ROOT / "core" / "templates" / name).read_text(encoding="utf-8")


def _rendered(name: str, language: str = "en") -> str:
    """The page as server_error() renders the 500: no request, no context."""
    with translation.override(language):
        return loader.get_template(name).render()


def _style(html: str) -> str:
    styles = re.findall(r"<style>(.*?)</style>", html, re.S)
    assert len(styles) == 1, "a page has one <style>"
    return styles[0]


def _scripts(html: str) -> list[str]:
    return re.findall(r"<script>(.*?)</script>", html, re.S)


def _norm(value: str) -> str:
    """One spelling per value: `rgba(10, 142, 110, 0.18)` is `rgba(10,142,110,0.18)`."""
    value = re.sub(r"\s*,\s*", ",", " ".join(value.split()))
    return re.sub(r"\s*\)", ")", re.sub(r"\(\s*", "(", value)).lower()


def _tokens(css: str) -> dict[tuple[tuple[str, ...], str], dict[str, str]]:
    """Custom properties by scope, (media conditions, selector); a later
    declaration in the same scope wins, as it does in the cascade."""
    # An @import before the first rule would read as the head of a block and
    # hide the :root that follows it from the rule parser. (Its quoted URL has
    # semicolons of its own: `wght@300;400;…`.)
    css = re.sub(r"""@(?:import|charset)\b(?:[^;'"]|'[^']*'|"[^"]*")*;""", "", css)
    scopes: dict[tuple[tuple[str, ...], str], dict[str, str]] = {}
    for media, selector, body, _ in _rules(css):
        if selector not in THEMED:
            continue
        scope = (tuple(" ".join(m.split()) for m in media), selector)
        for prop, value in _declarations(body):
            if prop.startswith("--"):
                scopes.setdefault(scope, {})[prop] = _norm(value)
    return scopes


@functools.cache
def _global_tokens():
    return _tokens((ROOT / "static/css/global.css").read_text(encoding="utf-8"))


@functools.cache
def _page_css() -> str:
    return _style(_rendered("404.html"))


def _used(css: str) -> set[str]:
    return set(re.findall(r"var\((--[\w-]+)", css))


# ── The tokens are global.css's ───────────────────────────────────────────────


def test_the_token_reader_sees_global_css() -> None:
    # A reader that found nothing would make every comparison below vacuous.
    found = _global_tokens()
    assert {"--bg", "--card", "--font-serif", "--control-h", "--btn-primary-bg"} <= set(
        found[LIGHT]
    )
    assert {"--bg", "--card", "--text-brand", "--btn-primary-bg"} <= set(found[DARK])
    assert "--control-lh" in found[((), 'html[dir="rtl"]')]
    assert "--control-h" in found[(("@media (max-width: 768px), (pointer: coarse)",), ":root")]


def test_every_token_a_page_reads_is_declared_on_the_page() -> None:
    css = _page_css()
    assert _used(css) - set(_tokens(css)[LIGHT]) == set()


def test_the_pages_carry_global_css_tokens_scope_for_scope() -> None:
    """Every token the pages read, with global.css's value, in every scope where
    global.css sets it — and nothing global.css does not set."""
    css = _page_css()
    used = _used(css)
    expected = {}
    for scope, declared in _global_tokens().items():
        mine = {name: value for name, value in declared.items() if name in used}
        if mine:
            expected[scope] = mine
    page = _tokens(css)
    drift = [
        f"{scope}: {name} is {page.get(scope, {}).get(name)!r} here, "
        f"{expected.get(scope, {}).get(name)!r} in global.css"
        for scope in sorted(set(page) | set(expected))
        for name in sorted(set(page.get(scope, {})) | set(expected.get(scope, {})))
        if page.get(scope, {}).get(name) != expected.get(scope, {}).get(name)
    ]
    assert drift == [], "\n".join(drift)


def test_the_two_pages_share_one_stylesheet_and_their_scripts() -> None:
    first, second = (_rendered(name) for name in PAGES)
    assert _style(first) == _style(second)
    assert _scripts(first) == _scripts(second)
    assert len(_scripts(first)) == 2  # the theme before paint, the language switch


# ── The look is the system's ──────────────────────────────────────────────────

#: The rules the brief names: the paper ground and the white card, the
#: masthead's serif headline at its one weight, Inter, the type scale, and the
#: default tier of the control ladder in the primary pair.
SYSTEM = {
    "html": {"font-family": "var(--font-body)", "font-size": "var(--text-md)"},
    "body": {"background": "var(--bg)"},
    ".ep-card": {
        "background": "var(--card)",
        "border": "1px solid var(--line)",
        "border-radius": "var(--radius-lg)",
        "box-shadow": "var(--shadow-card)",
    },
    ".ep-kicker": {"font-size": "var(--text-xs)"},
    ".ep-title": {
        "font-family": "var(--font-serif)",
        "font-weight": "400",
        "font-size": "var(--text-display)",
    },
    ".ep-desc": {"font-size": "var(--text-base)"},
    ".ep-btn": {
        "min-height": "var(--control-h)",
        "padding": "var(--space-2) var(--control-px)",
        "border-radius": "var(--control-radius)",
        "font-size": "var(--control-fs)",
        "line-height": "var(--control-lh)",
    },
    ".ep-btn-primary": {"background": "var(--btn-primary-bg)", "color": "var(--btn-primary-ink)"},
    ".ep-btn-primary:hover": {"background": "var(--btn-primary-hover)"},
}


def test_the_page_is_drawn_in_the_design_system() -> None:
    top: dict[str, dict[str, str]] = {}
    for media, selector, body, _ in _rules(_page_css()):
        if not media:
            top.setdefault(selector, {}).update(_declarations(body))
    wrong = [
        f"{selector} {{ {prop}: {top.get(selector, {}).get(prop)} }}, wanted {value}"
        for selector, wanted in SYSTEM.items()
        for prop, value in wanted.items()
        if top.get(selector, {}).get(prop) != value
    ]
    assert wrong == [], "\n".join(wrong)


COLOUR_LITERAL = re.compile(
    r"#[0-9a-f]{3,8}\b|\b(?:rgba?|hsla?|hwb|lab|lch|oklab|oklch)\(|\b(?:white|black)\b", re.I
)


def test_outside_the_token_blocks_every_colour_is_a_token() -> None:
    # The old pages painted #0A8E6E, #00AEDA, #C03030 and #fff into their rules,
    # which is how they missed dark mode altogether.
    found = [
        f"{selector} {{ {prop}: {value} }}"
        for _, selector, body, _ in _rules(_page_css())
        if selector not in THEMED
        for prop, value in _declarations(body)
        if COLOUR_LITERAL.search(value)
    ]
    assert found == [], "\n".join(found)


# ── Contrast ─────────────────────────────────────────────────────────────────

#: Every text colour the pages paint, on what it sits on: the card, unless it is
#: a button's own fill or a mark's tint. The marks' glyphs are drawn, not set
#: (3:1 would do); they are held to 4.5:1 with the text anyway.
TEXT_ON = [
    ("var(--t0)", "var(--card)"),  # the headline
    ("var(--t2)", "var(--card)"),  # the body's own colour
    ("var(--t3)", "var(--card)"),  # the description
    ("var(--text-brand)", "var(--card)"),  # the 404 kicker, the language switch
    ("var(--sev-critical)", "var(--card)"),  # the 500 kicker
    ("var(--btn-primary-ink)", "var(--btn-primary-bg)"),
    ("var(--btn-primary-ink)", "var(--btn-primary-hover)"),
    ("var(--btn-neutral-text)", "var(--btn-neutral-bg)"),
    ("var(--text-brand)", "color-mix(in srgb,var(--teal) 10%,var(--btn-neutral-bg))"),
    ("var(--text-brand)", "color-mix(in srgb,var(--teal) 10%,var(--card))"),  # the 404 mark
    ("var(--sev-critical)", "var(--sev-critical-bg)"),  # the 500 mark
]


def _colour(tokens: dict[str, str], expr: str) -> tuple[float, float, float, float]:
    """An sRGB colour with alpha, from a token, a hex, rgb()/rgba(), or a
    color-mix() in srgb (premultiplied, as CSS Color 5 mixes)."""
    expr = _norm(expr)
    if var := re.fullmatch(r"var\((--[\w-]+)\)", expr):
        return _colour(tokens, tokens[var.group(1)])
    if hexa := re.fullmatch(r"#([0-9a-f]{6})", expr):
        h = hexa.group(1)
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 1.0)
    if rgb := re.fullmatch(r"rgba?\(([^)]*)\)", expr):
        parts = [float(p) for p in rgb.group(1).split(",")]
        return (parts[0], parts[1], parts[2], parts[3] if len(parts) > 3 else 1.0)
    if mix := re.fullmatch(r"color-mix\(in srgb,(var\([\w-]+\)) (\d+)%,(var\([\w-]+\))\)", expr):
        first, second = _colour(tokens, mix.group(1)), _colour(tokens, mix.group(3))
        p = int(mix.group(2)) / 100
        alpha = first[3] * p + second[3] * (1 - p)
        rgb3 = [
            (first[i] * first[3] * p + second[i] * second[3] * (1 - p)) / alpha for i in range(3)
        ]
        return (rgb3[0], rgb3[1], rgb3[2], alpha)
    raise AssertionError(f"no colour reader for {expr!r}")


def _over(top: tuple, under: tuple) -> tuple[float, float, float, float]:
    a = top[3]
    return (*(top[i] * a + under[i] * (1 - a) for i in range(3)), 1.0)


def _ratio(one: tuple, two: tuple) -> float:
    def luminance(c: tuple) -> float:
        def channel(v: float) -> float:
            v /= 255
            return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4

        return 0.2126 * channel(c[0]) + 0.7152 * channel(c[1]) + 0.0722 * channel(c[2])

    hi, lo = sorted((luminance(one), luminance(two)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_the_contrast_table_covers_every_text_colour_painted() -> None:
    painted = {
        value
        for _, selector, body, _ in _rules(_page_css())
        if selector not in THEMED
        for prop, value in _declarations(body)
        if prop == "color"
    }
    assert painted - {ink for ink, _ in TEXT_ON} == set()


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize(("ink", "ground"), TEXT_ON)
def test_every_text_colour_clears_4_5_in_both_themes(theme: str, ink: str, ground: str) -> None:
    scopes = _tokens(_page_css())
    tokens = {**scopes[LIGHT], **(scopes[DARK] if theme == "dark" else {})}
    card = _colour(tokens, "var(--card)")
    under = _over(_colour(tokens, ground), card)
    ratio = _ratio(_over(_colour(tokens, ink), under), under)
    assert ratio >= 4.5, f"{theme}: {ink} on {ground} is {ratio:.2f}:1"


# ── The theme, as base.html applies it ───────────────────────────────────────

#: base.html's anti-flash script, line for line: the storage key, the fallback
#: to the system's preference, the attribute every dark rule hangs on.
THEME_LINES = (
    "var storageKey = studentPortal ? 'student-theme' : 'theme';",
    "try { t = localStorage.getItem(storageKey); } catch (e) {}",
    "if (t !== 'dark' && t !== 'light') {",
    "t = studentPortal ? 'light' : (window.matchMedia('(prefers-color-scheme:dark)').matches"
    " ? 'dark' : 'light');",
    "root.setAttribute('data-theme', t);",
)


@pytest.mark.parametrize("name", PAGES)
def test_the_saved_theme_is_applied_as_base_html_applies_it(name: str) -> None:
    base = (ROOT / "core/templates/core/base.html").read_text(encoding="utf-8")
    html = _rendered(name)
    before_paint = html[: html.index("<style>")]
    for line in THEME_LINES:
        assert line in base, f"base.html no longer says {line!r}: bring the error pages with it"
        assert line in before_paint, f"{name} does not apply the theme before it paints: {line!r}"
    # base.html reads the portal from the request; an error page has none.
    assert "window.location.pathname.slice(0, 9) === '/student/'" in before_paint


# ── Standalone, and in the app's language ────────────────────────────────────


@pytest.mark.parametrize("name", PAGES)
def test_a_page_needs_nothing_but_the_template_engine(name: str) -> None:
    source = _source(name)
    tags = set(re.findall(r"\{%\s*(\w+)", source))
    assert tags <= {"load", "get_current_language", "if", "else", "endif", "comment", "endcomment"}
    assert re.findall(r"\{%\s*load\s+(.*?)\s*%\}", source) == ["i18n"]
    assert "{{" not in source, "a variable needs a context, and the 500 is given none"
    for language, direction in (("en", "ltr"), ("ar", "rtl")):
        assert f'<html lang="{language}" dir="{direction}">' in _rendered(name, language)


def _boom(request):
    raise RuntimeError("deliberate: the 500 page under test")


urlpatterns = [path("boom/", _boom)]


@pytest.mark.parametrize(("url", "status"), [("/no-such-page/", 404), ("/boom/", 500)])
@pytest.mark.parametrize(
    ("cookie", "accept", "language"),
    [
        (None, None, "en"),  # the site's default
        (None, "ar", "ar"),  # the browser asks for Arabic
        ("ar", None, "ar"),  # the user chose Arabic
        ("en", "ar", "en"),  # ...and the user's choice beats the browser
    ],
)
def test_the_pages_open_in_the_language_the_app_renders(
    settings, url: str, status: int, cookie: str | None, accept: str | None, language: str
) -> None:
    settings.ROOT_URLCONF = __name__
    client = Client(raise_request_exception=False)
    if cookie:
        client.cookies[django_settings.LANGUAGE_COOKIE_NAME] = cookie
    headers = {"HTTP_ACCEPT_LANGUAGE": accept} if accept else {}
    response = client.get(url, **headers)
    assert response.status_code == status
    html = response.content.decode()
    direction = "rtl" if language == "ar" else "ltr"
    assert f'<html lang="{language}" dir="{direction}">' in html
    title = re.search(r"<title>(.*?)</title>", html).group(1)
    assert title.startswith(str(status))
    assert bool(re.search("[\u0600-\u06ff]", title)) == (language == "ar"), title

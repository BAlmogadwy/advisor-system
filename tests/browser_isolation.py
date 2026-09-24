"""Browser tests reach nothing beyond the test server.

`static/css/global.css` @imports Google Fonts, and in a browser test that
request went to the real internet. Every `page.goto` waited for it - the load
event waits for stylesheets, their @imports and the fonts they start - and
every wait for "networkidle" waited for it too. So a slow or stalled answer
from Google (a busy machine, a flaky network, a CI runner) failed tests that
test nothing about fonts; the portfolio page's "networkidle" once ran out its
30 s that way in a full-suite run.

Here the fonts' stylesheet is answered locally and empty: the page falls back
to the fonts installed on the host, and no font file is ever asked for. Any
other request that would leave the test server is refused. Each local answer
carries `ISOLATION_HEADER`, so a test can tell it from a real one.

`install()` - called by tests/conftest.py - does this for every browser context
opened through Playwright's SYNC API: `Browser.new_context`, `Browser.new_page`
(which opens a context of its own) and `BrowserType.launch_persistent_context`.
It covers every session that loads tests/conftest.py - any full run, any run of
a file under tests/. Not covered: contexts opened through `playwright.async_api`
(no test does), and a browser test collected under core/ run on its own.
"""

from __future__ import annotations

import functools
import re
from urllib.parse import urlsplit

GOOGLE_FONTS_CSS_HOST = "fonts.googleapis.com"
ISOLATION_HEADER = "x-test-isolation"

# Anything over http(s) that is not the test server on the loopback.
OFF_SITE = re.compile(r"^https?://(?!(?:localhost|127\.0\.0\.1|\[::1\])(?::\d+)?(?:/|$))")


def _answer(route) -> None:
    if urlsplit(route.request.url).hostname == GOOGLE_FONTS_CSS_HOST:
        route.fulfill(
            status=200,
            content_type="text/css",
            body="",
            headers={ISOLATION_HEADER: "answered-locally"},
        )
    else:
        route.abort("blockedbyclient")


def isolate(context):
    """Keep a Playwright browser context on the test server; returns it."""
    context.route(OFF_SITE, _answer)
    return context


def install() -> None:
    """Isolate every context Playwright's sync API opens from now on."""
    try:
        from playwright.sync_api import Browser, BrowserType
    except ImportError:  # Playwright is optional: its tests skip without it.
        return
    if getattr(Browser, "_test_isolation_installed", False):
        return
    new_context = Browser.new_context
    new_page = Browser.new_page
    launch_persistent_context = BrowserType.launch_persistent_context

    @functools.wraps(new_context)
    def isolated_new_context(self, *args, **kwargs):
        return isolate(new_context(self, *args, **kwargs))

    @functools.wraps(new_page)
    def isolated_new_page(self, *args, **kwargs):
        page = new_page(self, *args, **kwargs)
        isolate(page.context)
        return page

    @functools.wraps(launch_persistent_context)
    def isolated_launch_persistent_context(self, *args, **kwargs):
        return isolate(launch_persistent_context(self, *args, **kwargs))

    Browser.new_context = isolated_new_context
    Browser.new_page = isolated_new_page
    BrowserType.launch_persistent_context = isolated_launch_persistent_context
    Browser._test_isolation_installed = True

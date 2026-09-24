"""Browser tests never wait on the internet.

`global.css` @imports Google Fonts; a browser test that let that request out
waited on Google for every page load and every "networkidle" - and a slow
answer once ran the portfolio test's 30 s out in a full-suite run. See
`tests/browser_isolation.py`; tests/conftest.py installs it for every test.

These open their contexts the plain way - no `isolate()` call - because that is
what every other browser test does: what is proved here is the installation.
"""

from __future__ import annotations

import os
import tempfile

# See tests/test_advisor_browser.py: Playwright's sync API runs a greenlet loop,
# which Django mistakes for an async context and blocks every ORM call.
os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "1")

import pytest
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.urls import reverse

from tests.browser_isolation import GOOGLE_FONTS_CSS_HOST, ISOLATION_HEADER, OFF_SITE


@pytest.mark.parametrize(
    ("url", "off_site"),
    [
        ("http://localhost:61234/login/", False),
        ("http://localhost/static/css/global.css", False),
        ("http://127.0.0.1:8081/report/advisors/", False),
        ("http://[::1]:9000/", False),
        ("http://localhost:61234", False),
        ("https://fonts.googleapis.com/css2?family=Inter", True),
        ("https://fonts.gstatic.com/s/inter/v1/a.woff2", True),
        ("https://cdn.example.org/lib.js", True),
        # A lookalike host is not the test server.
        ("http://localhost.example.org/", True),
        ("http://127.0.0.1.example.org/", True),
    ],
)
def test_only_the_test_server_is_on_site(url: str, off_site: bool) -> None:
    assert bool(OFF_SITE.match(url)) is off_site


playwright = pytest.importorskip("playwright.sync_api")


class BrowserIsolationTests(StaticLiveServerTestCase):
    """Real pages, opened the way any test opens them."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._pw = playwright.sync_playwright().start()
        cls.browser = cls._pw.chromium.launch()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls._pw.stop()
        super().tearDownClass()

    def _load_login(self, page) -> tuple[list[str], dict[str, dict[str, str]]]:
        requests: list[str] = []
        answers: dict[str, dict[str, str]] = {}
        page.on("request", lambda request: requests.append(request.url))
        page.on("response", lambda response: answers.__setitem__(response.url, response.headers))
        page.goto(f"{self.live_server_url}{reverse('login')}", timeout=15_000)
        page.wait_for_load_state("networkidle", timeout=15_000)
        return requests, answers

    def _assert_fonts_answered_locally(self, requests, answers) -> None:
        off_site = [url for url in requests if OFF_SITE.match(url)]
        fonts_css = [url for url in off_site if GOOGLE_FONTS_CSS_HOST in url]
        assert fonts_css, "global.css no longer asks for Google Fonts: this test proves nothing"
        for url in fonts_css:
            assert answers[url].get(ISOLATION_HEADER) == "answered-locally", url
        # The stylesheet came back empty, so no font file was ever asked for.
        assert not [url for url in off_site if GOOGLE_FONTS_CSS_HOST not in url], off_site

    def test_a_plain_new_context_is_isolated(self) -> None:
        context = self.browser.new_context()
        self.addCleanup(context.close)
        self._assert_fonts_answered_locally(*self._load_login(context.new_page()))

    def test_a_page_opened_straight_from_the_browser_is_isolated(self) -> None:
        # Browser.new_page opens a context of its own.
        page = self.browser.new_page()
        self.addCleanup(page.close)
        self._assert_fonts_answered_locally(*self._load_login(page))

    def test_a_persistent_context_is_isolated(self) -> None:
        profile = tempfile.TemporaryDirectory()
        self.addCleanup(profile.cleanup)
        context = self._pw.chromium.launch_persistent_context(profile.name)
        self.addCleanup(context.close)
        page = context.pages[0] if context.pages else context.new_page()
        self._assert_fonts_answered_locally(*self._load_login(page))

    def test_any_other_host_is_refused_and_the_test_server_is_not(self) -> None:
        context = self.browser.new_context()
        self.addCleanup(context.close)
        page = context.new_page()
        page.goto(f"{self.live_server_url}{reverse('login')}", timeout=15_000)
        failures: dict[str, str | None] = {}
        page.on("requestfailed", lambda request: failures.__setitem__(request.url, request.failure))
        # A host under the reserved .invalid name: it could never be reached,
        # so only the refusal's reason tells isolation from a failed lookup.
        outside = "https://cdn.isolation-test.invalid/lib.js"
        said = page.evaluate("url => fetch(url).then(() => 'answered', () => 'refused')", outside)
        assert said == "refused"
        # Chromium names it "net::ERR_BLOCKED_BY_CLIENT.Inspector"; a lookup
        # that failed would say "net::ERR_NAME_NOT_RESOLVED".
        assert (failures.get(outside) or "").startswith("net::ERR_BLOCKED_BY_CLIENT"), failures
        inside = page.evaluate(
            "url => fetch(url).then(response => response.status)",
            f"{self.live_server_url}{reverse('login')}",
        )
        assert inside == 200

"""Browser acceptance coverage for the database-roster scraper controls.

Every scraper request is intercepted in Chromium.  These tests exercise the
rendered dashboard and its real JavaScript without allowing a request to reach
the process-launching view.
"""

from __future__ import annotations

import json
import os
from urllib.parse import parse_qs, urlsplit

# Playwright's synchronous API uses a greenlet loop.  The ORM work below is
# synchronous fixture setup; Django's live server handles requests separately.
os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "1")

import pytest  # noqa: E402
from django.conf import settings  # noqa: E402
from django.contrib.auth.models import User  # noqa: E402
from django.contrib.staticfiles.testing import StaticLiveServerTestCase  # noqa: E402
from django.test import Client  # noqa: E402
from django.urls import reverse  # noqa: E402

playwright_api = pytest.importorskip("playwright.sync_api")
sync_playwright = playwright_api.sync_playwright

ROSTER_TOKEN = "browser-test-database-roster-token"
DATABASE_SUMMARY = {
    "ok": True,
    "database": {
        "total": 1527,
        "valid": 1396,
        "excluded": 131,
        "invalid": 0,
        "ready": True,
        "excluded_reasons": {"status_not_in_scope": 130, "non_portal_student_id": 1},
        "roster_token": ROSTER_TOKEN,
    },
}
IDLE_STATUS = {
    "running": False,
    "pid": None,
    "started_at": None,
    "stopped_at": None,
    "last_action": "idle",
    "log_path": "browser-test-stub.log",
    "log_tail": "",
    "history": [],
}


class ScrapeDashboardBrowserTests(StaticLiveServerTestCase):
    """Drive the super-admin scraper panel through its rendered controls."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._pw = sync_playwright().start()
        cls.browser = cls._pw.chromium.launch()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls._pw.stop()
        super().tearDownClass()

    def _session_cookie(self) -> dict[str, str]:
        user = User.objects.create_superuser(
            username="scrape-browser-admin",
            email="scrape-browser@example.test",
            password="unused",
        )
        client = Client()
        client.force_login(user)
        return {
            "name": settings.SESSION_COOKIE_NAME,
            "value": client.cookies[settings.SESSION_COOKIE_NAME].value,
            "url": self.live_server_url,
        }

    def _new_page(self, session_cookie: dict[str, str]):
        context = self.browser.new_context(
            locale="en-US",
            extra_http_headers={"Accept-Language": "en"},
        )
        context.add_cookies(
            [
                session_cookie,
                {"name": settings.LANGUAGE_COOKIE_NAME, "value": "en", "url": self.live_server_url},
            ]
        )
        return context, context.new_page()

    @staticmethod
    def _fulfill_mode(route, mode: str, success_body: dict[str, object]) -> None:
        if mode == "ok":
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(success_body),
            )
            return
        if mode == "network":
            route.abort("failed")
            return
        status = int(mode)
        route.fulfill(
            status=status,
            content_type="application/json",
            body=json.dumps({"ok": False, "error": f"stubbed HTTP {status}"}),
        )

    def _stub_scrape_routes(
        self,
        page,
        state: dict[str, str],
        start_requests: list[dict[str, object]],
    ) -> None:
        def route_scrape_api(route, request) -> None:
            path = urlsplit(request.url).path
            if path == "/ops/scrape/source-summary/":
                self._fulfill_mode(route, state["summary"], DATABASE_SUMMARY)
                return
            if path == "/ops/scrape/status/":
                self._fulfill_mode(route, state["status"], IDLE_STATUS)
                return
            if path == "/ops/scrape/start/":
                start_requests.append(
                    {
                        "headers": dict(request.headers),
                        "post_data": request.post_data or "",
                    }
                )
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"ok": True, "pid": 4242}),
                )
                return

            # Stop and Oracle-import calls are not part of these scenarios.  Fail
            # closed if a regression unexpectedly issues either request.
            route.fulfill(
                status=500,
                content_type="application/json",
                body=json.dumps({"ok": False, "error": "unexpected scraper request"}),
            )

        page.route("**/ops/scrape/**", route_scrape_api)

    def _open_scrape_panel(self, page) -> None:
        page.goto(f"{self.live_server_url}{reverse('dashboard')}#scrape", wait_until="load")
        page.wait_for_selector("#scrape.active")

    @staticmethod
    def _wait_for_initial_failures(page) -> None:
        page.wait_for_function(
            """() =>
                document.querySelector('#scrapeSourceMeta')?.classList.contains('meta-danger') &&
                document.querySelector('#scrapeMeta')?.classList.contains('meta-danger')
            """
        )

    def test_database_and_csv_sources_require_readiness_and_submit_safe_payloads(self) -> None:
        session_cookie = self._session_cookie()
        context, page = self._new_page(session_cookie)
        self.addCleanup(context.close)
        state = {"summary": "503", "status": "503"}
        start_requests: list[dict[str, object]] = []
        self._stub_scrape_routes(page, state, start_requests)

        self._open_scrape_panel(page)
        self._wait_for_initial_failures(page)

        start = page.locator("#scrapeStart")
        assert page.input_value("#scrapeSource") == "database"
        assert page.locator("#scrapeCsvWrap").is_hidden()
        assert page.locator("#scrapeCsv").is_disabled()
        assert start.is_disabled()

        # A valid roster alone is insufficient: status must also be known and idle.
        state["summary"] = "ok"
        page.select_option("#scrapeSource", "csv")
        page.wait_for_function(
            "() => document.querySelector('#scrapeSourceMeta')?.textContent.includes('selected CSV')"
        )
        page.select_option("#scrapeSource", "database")
        page.wait_for_function(
            """() =>
                document.querySelector('#scrapeSourceMeta')?.textContent.includes('1396 current students') &&
                document.querySelector('#scrapeSourceMeta')?.textContent.includes('131 terminal')
            """
        )
        assert start.is_disabled(), "database readiness must not override an unknown scraper status"

        state["status"] = "ok"
        page.click("#scrapeRefresh")
        page.wait_for_function("() => !document.querySelector('#scrapeStart')?.disabled")
        assert "1396" in page.inner_text("#scrapeSourceMeta")
        assert "131" in page.inner_text("#scrapeSourceMeta")

        page.click("#scrapeStart")
        dialog = page.locator(".dlg-backdrop[aria-labelledby='dlg-title-text']")
        dialog.wait_for(state="visible")
        confirmation = dialog.locator(".dlg-body").inner_text()
        assert "1396 eligible, 131 excluded" in confirmation

        with page.expect_response(
            lambda response: urlsplit(response.url).path == "/ops/scrape/start/"
        ) as database_start:
            dialog.locator(".btn-confirm").click()
        assert database_start.value.status == 200
        assert len(start_requests) == 1

        database_request = start_requests[0]
        database_form = parse_qs(str(database_request["post_data"]), keep_blank_values=True)
        assert database_form["student_source"] == ["database"]
        assert database_form["students_csv"] == [""]
        assert database_form["database_roster_token"] == [ROSTER_TOKEN]

        csrf_cookie = next(
            cookie["value"]
            for cookie in context.cookies()
            if cookie["name"] == settings.CSRF_COOKIE_NAME
        )
        request_headers = database_request["headers"]
        assert isinstance(request_headers, dict)
        assert request_headers.get("x-csrftoken") == csrf_cookie

        dialog.wait_for(state="detached")
        page.wait_for_function("() => !document.querySelector('#scrapeStart')?.disabled")

        page.select_option("#scrapeSource", "csv")
        page.wait_for_function("() => !document.querySelector('#scrapeCsv')?.disabled")
        assert page.locator("#scrapeCsvWrap").is_visible()
        page.fill("#scrapeCsv", "data/students_list.csv")
        assert start.is_enabled()

        page.click("#scrapeStart")
        csv_dialog = page.locator(".dlg-backdrop[aria-labelledby='dlg-title-text']")
        csv_dialog.wait_for(state="visible")
        assert "data/students_list.csv" in csv_dialog.locator(".dlg-body").inner_text()

        with page.expect_response(
            lambda response: urlsplit(response.url).path == "/ops/scrape/start/"
        ) as csv_start:
            csv_dialog.locator(".btn-confirm").click()
        assert csv_start.value.status == 200
        assert len(start_requests) == 2

        csv_request = start_requests[1]
        csv_form = parse_qs(str(csv_request["post_data"]), keep_blank_values=True)
        assert csv_form["student_source"] == ["csv"]
        assert csv_form["students_csv"] == ["data/students_list.csv"]
        assert csv_form.get("database_roster_token") in (None, [""])

    def test_summary_and_status_failures_never_enable_database_start(self) -> None:
        session_cookie = self._session_cookie()
        failure_modes = ("401", "403", "409", "network")

        for endpoint in ("summary", "status"):
            for failure_mode in failure_modes:
                with self.subTest(endpoint=endpoint, failure_mode=failure_mode):
                    state = {"summary": "ok", "status": "ok"}
                    state[endpoint] = failure_mode
                    start_requests: list[dict[str, object]] = []
                    context, page = self._new_page(session_cookie)
                    try:
                        self._stub_scrape_routes(page, state, start_requests)
                        self._open_scrape_panel(page)
                        error_selector = (
                            "#scrapeSourceMeta.meta-danger"
                            if endpoint == "summary"
                            else "#scrapeMeta.meta-danger"
                        )
                        page.wait_for_selector(error_selector)

                        assert page.input_value("#scrapeSource") == "database"
                        assert page.locator("#scrapeStart").is_disabled()
                        assert start_requests == []
                    finally:
                        context.close()

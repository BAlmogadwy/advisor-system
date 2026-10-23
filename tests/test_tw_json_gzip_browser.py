"""Stage 3, B5 in a real browser: the classic workspace draws from gzipped
answers, in English and Arabic (the [CI] twin of the QA 665 probe in
``docs/stage3/B5.md``).

Chromium asks for gzip on every request, and the live server runs every
middleware in ``settings.MIDDLEWARE``, with ``TIMETABLE_WORKSPACE_JSON_GZIP`` on
(production keeps it off, because Render's load balancers already compress;
``tests/test_tw_json_gzip.py`` pins that). Resource Timing tells what came over the
wire: a gzipped answer's ``encodedBodySize`` is smaller than its
``decodedBodySize``; an answer sent as it is has the two equal. So:

* every ``/ops/tw/`` answer of at least 1 KB arrived compressed, the board
  read among them, and the page drew its cards from it with no page error;
* every smaller one arrived as it is;
* the HTML page itself arrived as it is.
"""

from __future__ import annotations

import os

# Playwright's synchronous API runs through a greenlet; fixture creation is
# synchronous ORM work while Django serves the page on its own thread.
os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "1")

import pytest
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import override_settings

from tests.timetable_workspace_browser_support import WorkspaceBrowserMixin

pytest.importorskip("playwright.sync_api")

WIRE = """() => ({
  page: (() => {
    const nav = performance.getEntriesByType('navigation')[0];
    return {encoded: nav.encodedBodySize, decoded: nav.decodedBodySize};
  })(),
  api: performance.getEntriesByType('resource')
    .filter(e => new URL(e.name).pathname.startsWith('/ops/tw/'))
    .map(e => ({path: new URL(e.name).pathname, encoded: e.encodedBodySize, decoded: e.decodedBodySize})),
})"""


@override_settings(TIMETABLE_WORKSPACE_JSON_GZIP=True)
class WorkspaceGzipBrowserTests(WorkspaceBrowserMixin, StaticLiveServerTestCase):
    def _assert_drawn_from_gzip(self) -> None:
        for language in ("en", "ar"):
            with self.subTest(language=language):
                page, sent, errors = self._load(language, "light", 1440, 790)
                wire = page.evaluate(WIRE)

                self.assertEqual(errors, [])
                self.assertEqual(sent, [], "the page only reads")
                self.assertGreater(wire["page"]["decoded"], 1024)
                self.assertEqual(
                    wire["page"]["encoded"],
                    wire["page"]["decoded"],
                    "the HTML page is never gzipped",
                )
                board = f"/ops/tw/boards/{self.scenario.boards.get().id}/"
                self.assertIn(board, {e["path"] for e in wire["api"]})
                for entry in wire["api"]:
                    if entry["decoded"] >= 1024:
                        self.assertLess(entry["encoded"], entry["decoded"], entry)
                    else:
                        self.assertEqual(entry["encoded"], entry["decoded"], entry)
                big = [e for e in wire["api"] if e["decoded"] >= 1024]
                self.assertIn(board, {e["path"] for e in big}, wire["api"])

    def test_the_workspace_draws_from_gzipped_answers(self) -> None:
        self._assert_drawn_from_gzip()

    @override_settings(
        TIMETABLE_ONE_TEACHER_PER_SECTION=True,
        TIMETABLE_INSTRUCTOR_DAY_OFF_POLICY=True,
        TIMETABLE_SECTION_PROGRAMME_PREFERENCE=True,
    )
    def test_the_workspace_draws_from_gzipped_answers_under_the_production_flags(self) -> None:
        self._assert_drawn_from_gzip()

"""Browser contracts for the Telegram timetable screenshot.

These are intentionally narrower than the adviser browser suite: the card is a
private, JavaScript-rendered document and the output students receive is a PNG.
An HTTP assertion can prove the data reached the page, but not that WeekGrid
actually retained the meeting boundaries or that baseline-only rows survived
the conversion from the semantic list to the visual grid.
"""

from __future__ import annotations

import os
import struct
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any, ClassVar, TypeAlias

os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "1")

import pytest
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import LiveServerTestCase, override_settings

from core.models import AdvisorConversation, AdvisorMessage
from core.services.advisor_presentations import KIND_GRADUATION, KIND_TIMETABLE
from telegram_gateway.cards import sign_card, sign_renderer_request

if TYPE_CHECKING:
    from playwright.sync_api import Browser, Page, Playwright

StartResponse: TypeAlias = Callable[..., Callable[[bytes], object]]
WSGIApplication: TypeAlias = Callable[[dict[str, Any], StartResponse], Iterable[bytes]]

playwright_api = pytest.importorskip("playwright.sync_api")
sync_playwright = playwright_api.sync_playwright


class _NoStaticInterception:
    """Let Django route signed paths containing ':' on Windows.

    Django's test static handler runs every URL through ``nturl2path`` first;
    that parser treats the signature separators as malformed drive letters.
    Card assets already have their own authenticated Django route, so no test
    static-file interception is needed here.
    """

    def __init__(self, application: WSGIApplication) -> None:
        self.application = application

    def __call__(
        self,
        environ: dict[str, Any],
        start_response: StartResponse,
    ) -> Iterable[bytes]:
        return self.application(environ, start_response)


@override_settings(
    TELEGRAM_ADVISOR_ENABLED=True,
    TELEGRAM_SEND_TIMETABLE_IMAGES=True,
    TELEGRAM_SEND_GRADUATION_IMAGES=True,
    MEDIA_URL="/media/",
)
class TelegramCardBrowserTests(LiveServerTestCase):
    static_handler = _NoStaticInterception
    _pw: ClassVar[Playwright]
    browser: ClassVar[Browser]

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

    def _open_card(
        self,
        presentation: dict[str, Any],
        *,
        option_index: int | None = None,
    ) -> Page:
        conversation = AdvisorConversation.objects.create(student_id=7990001)
        message = AdvisorMessage.objects.create(
            conversation=conversation,
            role=AdvisorMessage.ROLE_ASSISTANT,
            content="Here is your timetable.",
            presentation=presentation,
            status=AdvisorMessage.STATUS_COMPLETED,
        )
        token = sign_card(message_id=message.pk, option_index=option_index)
        context = self.browser.new_context(
            viewport={"width": 760, "height": 900},
            device_scale_factor=2,
            extra_http_headers={"X-Telegram-Card-Renderer": sign_renderer_request()},
        )
        self.addCleanup(context.close)
        page = context.new_page()
        response = page.goto(f"{self.live_server_url}/telegram/card/{token}/")
        assert response is not None
        self.assertEqual(response.status, 200)
        page.wait_for_selector("#sa-card-root[data-card-ready='1']")
        self.assertIsNone(page.get_attribute("#sa-card-root", "data-card-error"))
        return page

    def test_option_grid_preserves_75_and_100_minute_boundaries(self) -> None:
        presentation = {
            "kind": KIND_TIMETABLE,
            "baseline_kind": "EMPTY",
            "alternatives": [
                {
                    "planner_options": ["A1"],
                    "scheduled_courses": 2,
                    "target_courses": 4,
                    "total_credit_hours": 7,
                    "meetings": [
                        {
                            "course_code": "AI331",
                            "course_name": "Applied Machine Learning and Intelligent Systems",
                            "section": "M2",
                            "day": "SUN",
                            "start": "09:00",
                            "end": "10:15",
                        },
                        {
                            "course_code": "CS285",
                            "course_name": "Software Engineering Principles and Practice",
                            "section": "M3",
                            "day": "MON",
                            "start": "09:00",
                            "end": "10:40",
                        },
                    ],
                    "unplaced_courses": [
                        {
                            "course_code": "CS211",
                            "course_name": "Algorithms and Data Structures",
                            "reason": (
                                "No section for this course is recorded in our data. "
                                "Check the registration portal."
                            ),
                        },
                        {
                            "course_code": "GSE1",
                            "course_name": "University Elective Course I",
                            "reason": (
                                "No section for this course is recorded in our data. "
                                "Check the registration portal."
                            ),
                        },
                    ],
                }
            ],
        }

        page = self._open_card(presentation, option_index=0)

        blocks = page.locator(".wg-blocks .wg-filled")
        self.assertEqual(blocks.count(), 2)
        seventy_five = page.locator(".wg-filled[data-start-minute='540'][data-end-minute='615']")
        one_hundred = page.locator(".wg-filled[data-start-minute='540'][data-end-minute='640']")
        self.assertEqual(seventy_five.count(), 1)
        self.assertEqual(one_hundred.count(), 1)
        seventy_five_style = seventy_five.get_attribute("style")
        one_hundred_style = one_hundred.get_attribute("style")
        assert seventy_five_style is not None
        assert one_hundred_style is not None
        self.assertIn("span 15", seventy_five_style)
        self.assertIn("span 20", one_hundred_style)
        self.assertIn("09:00–10:15", seventy_five.inner_text())
        self.assertIn("09:00–10:40", one_hundred.inner_text())

        for block in (seventy_five, one_hundred):
            time_node = block.locator(".sa-card-block-time")
            self.assertTrue(time_node.is_visible())
            block_box = block.bounding_box()
            time_box = time_node.bounding_box()
            assert block_box is not None
            assert time_box is not None
            self.assertGreaterEqual(time_box["x"], block_box["x"] - 1)
            self.assertGreaterEqual(time_box["y"], block_box["y"] - 1)
            self.assertLessEqual(
                time_box["x"] + time_box["width"],
                block_box["x"] + block_box["width"] + 1,
            )
            self.assertLessEqual(
                time_box["y"] + time_box["height"],
                block_box["y"] + block_box["height"] + 1,
            )

        minute_values = page.locator(".wg-blocks .wg-cell[data-minute]").evaluate_all(
            "nodes => nodes.map(node => Number(node.dataset.minute))"
        )
        self.assertEqual(min(minute_values), 540)
        self.assertEqual(max(minute_values), 655)

        legend = page.locator(".sa-card-course-legend")
        self.assertEqual(legend.count(), 1)
        legend_rows = legend.locator(".sa-card-course-row")
        self.assertEqual(legend_rows.count(), 2)
        self.assertEqual(
            legend.locator("[data-course-code='AI331']")
            .inner_text()
            .count("Applied Machine Learning and Intelligent Systems"),
            1,
        )
        self.assertIn(
            "Software Engineering Principles and Practice",
            legend.locator("[data-course-code='CS285']").inner_text(),
        )

        filled_styles = blocks.evaluate_all(
            """nodes => nodes.map(node => {
              const style = getComputedStyle(node);
              return {
                background: style.backgroundColor,
                accent: style.borderInlineStartColor,
                classes: node.className
              };
            })"""
        )
        empty_background = page.locator(".wg-blocks .wg-cell:not(.wg-filled)").first.evaluate(
            "node => getComputedStyle(node).backgroundColor"
        )
        self.assertNotEqual(filled_styles[0]["background"], empty_background)
        self.assertNotEqual(filled_styles[1]["background"], empty_background)
        self.assertNotEqual(filled_styles[0]["background"], filled_styles[1]["background"])
        self.assertNotIn("rgba(0, 0, 0, 0)", {row["accent"] for row in filled_styles})
        self.assertNotEqual(filled_styles[0]["classes"], filled_styles[1]["classes"])

        unplaced = page.locator(".sa-tt-unplaced")
        unplaced_rows = unplaced.locator(".sa-card-unplaced-row")
        self.assertEqual(unplaced_rows.count(), 2)
        self.assertIn("CS211", unplaced_rows.nth(0).inner_text())
        self.assertIn("Algorithms and Data Structures", unplaced_rows.nth(0).inner_text())
        self.assertIn("Check the registration portal.", unplaced_rows.nth(0).inner_text())
        self.assertEqual(
            unplaced_rows.nth(0).locator(".sa-card-unplaced-reason").get_attribute("dir"),
            "auto",
        )
        row_heights = unplaced_rows.evaluate_all(
            "nodes => nodes.map(node => node.getBoundingClientRect().height)"
        )
        self.assertTrue(all(height <= 72 for height in row_heights))
        unplaced_box = unplaced.bounding_box()
        assert unplaced_box is not None
        self.assertLessEqual(unplaced_box["height"], 150)

        for selector in (
            "#sa-card-root",
            ".sa-timetable",
            ".sa-tt-option:not([hidden])",
            ".sa-tt-grid-blocks",
            ".sa-card-course-legend",
            ".sa-tt-unplaced",
        ):
            dimensions = page.locator(selector).evaluate(
                "node => ({clientWidth: node.clientWidth, scrollWidth: node.scrollWidth})"
            )
            self.assertLessEqual(dimensions["scrollWidth"], dimensions["clientWidth"] + 1)

        root_box = page.locator("#sa-card-root").bounding_box()
        assert root_box is not None
        self.assertEqual(root_box["width"], 720)
        for selector in (
            ".sa-tt-option:not([hidden])",
            ".sa-tt-grid-blocks",
            ".sa-card-course-legend",
            ".sa-tt-unplaced",
        ):
            box = page.locator(selector).bounding_box()
            assert box is not None
            self.assertGreaterEqual(box["x"], root_box["x"] - 1)
            self.assertGreaterEqual(box["y"], root_box["y"] - 1)
            self.assertLessEqual(box["x"] + box["width"], root_box["x"] + root_box["width"] + 1)
            self.assertLessEqual(box["y"] + box["height"], root_box["y"] + root_box["height"] + 1)

        png = page.locator("#sa-card-root").screenshot(type="png")
        png_width, png_height = struct.unpack(">II", png[16:24])
        self.assertEqual(png_width, 1440)
        self.assertLess(png_height, 1300)

    def test_graduation_card_renders_the_shared_map_and_marks_itself_ready(self) -> None:
        presentation = {
            "kind": KIND_GRADUATION,
            "program": "DS2",
            "planning_term": "1448/1",
            "simulation_completed": True,
            "lower_bound_terms_including_planning_baseline": 2,
            "max_credits_per_term": 18,
            "band_labels": {
                "1": "Planning baseline 1448/1",
                "2": "Projected 1448/2",
            },
            "graph": {
                "items": [
                    {
                        "course_code": "DS341",
                        "prerequisite_course_code": "DS225",
                    }
                ],
                "termOf": {"DS225": 1, "DS341": 2},
                "nameOf": {"DS225": "Data Mining", "DS341": "Data Governance"},
                "statusOf": {"DS225": "studying", "DS341": "open"},
                "extraNodes": ["DS225", "DS341"],
            },
            "unresolved_requirements": [],
            "read_only": True,
        }

        page = self._open_card(presentation)

        root = page.locator("#sa-card-root")
        self.assertEqual(root.get_attribute("data-card-ready"), "1")
        self.assertEqual(page.locator(".sa-graduation-map").count(), 1)
        self.assertEqual(page.locator(".sa-timetable").count(), 0)
        self.assertIn("DS341", page.locator(".sa-graduation-map").inner_text())
        self.assertEqual(page.locator(".sa-graduation-map .prereq-svg").count(), 1)
        self.assertTrue(page.locator(".sa-grad-desktop").is_visible())
        self.assertFalse(page.locator(".sa-grad-mobile").is_visible())
        self.assertFalse(page.locator(".sa-grad-expand").is_visible())
        self.assertFalse(page.locator(".sa-grad-toolbar .pg-modes").is_visible())
        dimensions = root.evaluate(
            "node => ({clientWidth: node.clientWidth, scrollWidth: node.scrollWidth})"
        )
        self.assertLessEqual(dimensions["scrollWidth"], dimensions["clientWidth"] + 1)
        png = root.screenshot(type="png")
        png_width, png_height = struct.unpack(">II", png[16:24])
        self.assertEqual(png_width, 1440)
        self.assertLess(png_height, 3000)

    def test_replacement_card_keeps_swap_banner_and_outside_plan_caution(self) -> None:
        presentation = {
            "kind": KIND_TIMETABLE,
            "planning_term": "1448/1",
            "mode": "certified_replacement",
            "baseline_kind": "EXPECTED_PLAN",
            "replacement": {
                "remove_course": {"course_code": "DS341", "credits": 3},
                "add_course": {"course_code": "CS285", "credits": 4},
                "outside_plan_addition": True,
                "academic_improvement": {"proven_improvement": True, "terms_saved": 1},
            },
            "alternatives": [
                {
                    "planner_options": ["A1"],
                    "scheduled_courses": 1,
                    "target_courses": 1,
                    "total_credit_hours": 4,
                    "courses": [{"course_code": "CS285", "section": "M3", "credits": 4}],
                    "meetings": [
                        {
                            "course_code": "CS285",
                            "section": "M3",
                            "day": "MON",
                            "start": "10:30",
                            "end": "11:45",
                        }
                    ],
                    "unplaced_courses": [],
                }
            ],
        }

        page = self._open_card(presentation, option_index=0)

        banner = page.locator(".sa-tt-replacement")
        self.assertEqual(banner.count(), 1)
        self.assertIn("Replace DS341 with CS285", " ".join(banner.inner_text().split()))
        self.assertIn(
            "outside your recorded study plan",
            page.locator(".sa-tt-replacement-caution").inner_text(),
        )
        self.assertLessEqual(
            banner.evaluate("node => node.scrollWidth - node.clientWidth"),
            1,
        )

    def test_baseline_grid_keeps_unscheduled_and_legacy_meeting_rows(self) -> None:
        presentation = {
            "kind": KIND_TIMETABLE,
            "baseline_kind": "REGISTERED",
            "baseline_sections": [
                {
                    "course_code": "AI331",
                    "course_name": "Machine Learning",
                    "section": "M2",
                    "meetings": ["SUN 09:00-10:15"],
                },
                {
                    "course_code": "CS999",
                    "course_name": "Training",
                    "section": "M1",
                    "meetings": [],
                },
                {
                    "course_code": "STAT307",
                    "course_name": "Statistics",
                    "section": "M4",
                    "meetings": ["Sunday at 11"],
                },
                {
                    "course_code": "DS225",
                    "course_name": "Data Mining",
                    "section": "M1",
                    "meetings": ["MON 09:00-10:40", "legacy Tuesday slot"],
                },
            ],
            "alternatives": [],
        }

        page = self._open_card(presentation)

        self.assertEqual(page.locator("details.sa-tt-current[open]").count(), 1)
        self.assertEqual(page.locator(".wg-blocks .wg-filled").count(), 2)
        grid_text = page.locator(".wg-blocks").inner_text()
        self.assertIn("AI331", grid_text)
        self.assertIn("DS225", grid_text)

        fallback = page.locator(".sa-tt-current-list[data-grid-fallback='1']")
        self.assertEqual(fallback.count(), 1)
        fallback_text = fallback.inner_text()
        self.assertIn("CS999", fallback_text)
        self.assertIn("STAT307", fallback_text)
        self.assertIn("Sunday at 11", fallback_text)
        self.assertIn("DS225", fallback_text)
        self.assertIn("legacy Tuesday slot", fallback_text)
        self.assertNotIn("SUN 09:00-10:15", fallback_text)
        self.assertNotIn("MON 09:00-10:40", fallback_text)


def test_card_source_selects_exact_shared_blocks_and_retains_grid_fallbacks() -> None:
    from pathlib import Path

    source = Path("telegram_gateway/templates/telegram_gateway/card.html").read_text(
        encoding="utf-8"
    )

    assert "window.WeekGrid.renderWeekGrid" in source
    assert "mode: 'blocks'" in source
    assert "step: 5" in source
    assert "padMinutes: 0" in source
    assert "majorHeight: 32" in source
    assert "sa-card-course-legend" in source
    assert "sa-card-unplaced-row" in source
    assert "data-grid-fallback" in source
    assert "retainUnmappedBaselineRows" in source


@override_settings(TELEGRAM_ADVISOR_ENABLED=True)
class TelegramLinkPageBrowserTests(StaticLiveServerTestCase):
    """One real-browser check for the custom body used by all link templates."""

    _pw: ClassVar[Playwright]
    browser: ClassVar[Browser]

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

    def test_result_page_skip_link_reaches_a_sensibly_wide_main_landmark(self) -> None:
        context = self.browser.new_context(viewport={"width": 1000, "height": 800})
        self.addCleanup(context.close)
        page = context.new_page()

        response = page.goto(f"{self.live_server_url}/telegram/link/not-a-real-token/")
        assert response is not None
        self.assertEqual(response.status, 404)

        main = page.locator("main#main-content.telegram-link-wrap")
        self.assertEqual(main.count(), 1)
        width = main.evaluate("node => node.getBoundingClientRect().width")
        self.assertGreater(width, 600)
        self.assertLessEqual(width, 720)

        skip = page.locator("a.skip-link")
        self.assertEqual(skip.get_attribute("href"), "#main-content")
        skip.focus()
        page.keyboard.press("Enter")
        page.wait_for_function("window.location.hash === '#main-content'")
        self.assertEqual(page.evaluate("document.activeElement.id"), "main-content")

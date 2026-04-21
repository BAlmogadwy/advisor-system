"""PR8 — async job UX tests.

Module-level red-at-c1 import tripwires are inside `TestPR8Tripwire` and
go green at the commit indicated on each test class. The PR7 backend is
assumed present (this PR only wires UI + thin client).

Commit turn-green map (for tracking):

- c1: TestPR8Tripwire (imports fail until later commits land modules)
- c2: TestPR8JSAdapterShim
- c3: TestStatusCardRendering, TestStatusPillClasses
- c4: TestPollingCadence, TestPollingTerminalStop
- c5: TestControlsHappyPath, TestDuplicateSubmitBlocked
- c6: TestFailedCancelledStates, TestBackendDisabledHides
- c7: TestAcceptancePack, TestParityHelper
- c8: TestFlagDefaultPostPromotion
"""

from __future__ import annotations

from django.test import SimpleTestCase, override_settings

PR8_FLAG = "TIMETABLE_PR8_ASYNC_JOB_UI_ENABLED"


class TestPR8Tripwire(SimpleTestCase):
    """Imports that must eventually resolve. Red at c1, green over c2-c7."""

    def test_js_adapter_shim_importable(self) -> None:
        # c2 lands core/static/core/js/pr8_async_job_adapter.js and a tiny
        # python helper exposing its path for {% static %}.
        from core.services.pr8_async_job_ui import JS_ADAPTER_PATH  # noqa: F401

    def test_template_partial_importable(self) -> None:
        # c3 lands the status card partial.
        from django.template.loader import get_template

        get_template("core/partials/pr8_async_job_card.html")

    def test_flag_helper_importable(self) -> None:
        from core.services.pr8_async_job_ui import is_async_job_ui_enabled  # noqa: F401

    def test_parity_helper_importable(self) -> None:
        # c7
        from core.services.pr8_parity import strip_pr8_ui_context  # noqa: F401


class TestPR8JSAdapterShim(SimpleTestCase):
    """c2 — python-side shim exposing the adapter's {% static %} path and
    four endpoint URLs so templates don't hardcode them."""

    def test_adapter_static_path_resolves(self) -> None:
        from core.services.pr8_async_job_ui import JS_ADAPTER_PATH

        self.assertTrue(JS_ADAPTER_PATH.endswith(".js"))
        self.assertIn("pr8", JS_ADAPTER_PATH)

    def test_endpoint_map_has_four_routes(self) -> None:
        from core.services.pr8_async_job_ui import endpoint_map

        routes = endpoint_map()
        self.assertIn("submit", routes)
        self.assertIn("poll", routes)
        self.assertIn("result", routes)
        self.assertIn("cancel", routes)


class TestStatusCardRendering(SimpleTestCase):
    """c3 — partial renders known status states. Green at c3."""

    def _render(self, ctx: dict) -> str:
        from django.template.loader import render_to_string

        return render_to_string("core/partials/pr8_async_job_card.html", ctx)

    def test_renders_no_active_job(self) -> None:
        html = self._render({"pr8_job": None, "TIMETABLE_PR8_ASYNC_JOB_UI_ENABLED": True})
        # Card present but no status pill with a terminal/running state.
        self.assertIn("pr8-async-job-card", html)

    def test_renders_status_pill_for_running(self) -> None:
        html = self._render(
            {
                "pr8_job": {"status": "running", "job_id": "abc", "last_stage_seen": "greedy"},
                "TIMETABLE_PR8_ASYNC_JOB_UI_ENABLED": True,
            }
        )
        self.assertIn("status-running", html)

    def test_renders_status_pill_for_succeeded(self) -> None:
        html = self._render(
            {
                "pr8_job": {"status": "succeeded", "job_id": "abc"},
                "TIMETABLE_PR8_ASYNC_JOB_UI_ENABLED": True,
            }
        )
        self.assertIn("status-succeeded", html)


class TestStatusPillClasses(SimpleTestCase):
    """c3 — every valid PR7 status has a pill class."""

    def test_all_five_statuses_have_classes(self) -> None:
        from core.services.pr8_async_job_ui import status_pill_class

        for st in ("queued", "running", "succeeded", "failed", "cancelled"):
            self.assertTrue(status_pill_class(st).startswith("status-"))


class TestPollingCadence(SimpleTestCase):
    """c4 — declared polling cadence constant + terminal-state helper."""

    def test_cadence_is_2s(self) -> None:
        from core.services.pr8_async_job_ui import POLL_INTERVAL_MS

        self.assertEqual(POLL_INTERVAL_MS, 2000)

    def test_is_terminal_helper(self) -> None:
        from core.services.pr8_async_job_ui import is_terminal_status

        for st in ("succeeded", "failed", "cancelled"):
            self.assertTrue(is_terminal_status(st))
        for st in ("queued", "running"):
            self.assertFalse(is_terminal_status(st))


class TestPollingTerminalStop(SimpleTestCase):
    """c4 — partial embeds the cadence constant into the rendered JS
    bootstrap so the adapter knows when to stop."""

    def test_partial_carries_poll_interval(self) -> None:
        from django.template.loader import render_to_string

        html = render_to_string(
            "core/partials/pr8_async_job_card.html",
            {"pr8_job": None, "TIMETABLE_PR8_ASYNC_JOB_UI_ENABLED": True},
        )
        self.assertIn("2000", html)


class TestControlsHappyPath(SimpleTestCase):
    """c5 — controls dispatch to the right endpoints. Covered via adapter
    shape only at this layer (live interactions exercised in Chrome smoke)."""

    def test_submit_control_markup_present_when_enabled(self) -> None:
        from django.template.loader import render_to_string

        html = render_to_string(
            "core/partials/pr8_async_job_card.html",
            {"pr8_job": None, "TIMETABLE_PR8_ASYNC_JOB_UI_ENABLED": True},
        )
        self.assertIn('data-pr8-action="submit"', html)


class TestDuplicateSubmitBlocked(SimpleTestCase):
    """c5 — the submit control is marked disabled when a job is active."""

    def test_submit_disabled_while_running(self) -> None:
        from django.template.loader import render_to_string

        html = render_to_string(
            "core/partials/pr8_async_job_card.html",
            {
                "pr8_job": {"status": "running", "job_id": "abc"},
                "TIMETABLE_PR8_ASYNC_JOB_UI_ENABLED": True,
            },
        )
        # Either a disabled submit or no submit at all — both satisfy the bar.
        self.assertTrue(
            'data-pr8-action="submit" disabled' in html or 'data-pr8-action="submit"' not in html
        )


class TestFailedCancelledStates(SimpleTestCase):
    """c6 — failed / cancelled surfaces carry a Run-again control."""

    def _render(self, status: str) -> str:
        from django.template.loader import render_to_string

        return render_to_string(
            "core/partials/pr8_async_job_card.html",
            {
                "pr8_job": {"status": status, "job_id": "abc", "error_message": "boom"},
                "TIMETABLE_PR8_ASYNC_JOB_UI_ENABLED": True,
            },
        )

    def test_failed_shows_rerun(self) -> None:
        self.assertIn('data-pr8-action="rerun"', self._render("failed"))

    def test_cancelled_shows_rerun(self) -> None:
        self.assertIn('data-pr8-action="rerun"', self._render("cancelled"))


class TestBackendDisabledHides(SimpleTestCase):
    """c6 — if PR7 backend flag is off, PR8 UI must hide."""

    @override_settings(
        TIMETABLE_PR7_ASYNC_PLANNER_ENABLED=False,
        TIMETABLE_PR8_ASYNC_JOB_UI_ENABLED=True,
    )
    def test_hides_when_pr7_off(self) -> None:
        from core.services.pr8_async_job_ui import is_async_job_ui_effective

        self.assertFalse(is_async_job_ui_effective())

    @override_settings(
        TIMETABLE_PR7_ASYNC_PLANNER_ENABLED=True,
        TIMETABLE_PR8_ASYNC_JOB_UI_ENABLED=False,
    )
    def test_hides_when_pr8_off(self) -> None:
        from core.services.pr8_async_job_ui import is_async_job_ui_effective

        self.assertFalse(is_async_job_ui_effective())

    @override_settings(
        TIMETABLE_PR7_ASYNC_PLANNER_ENABLED=True,
        TIMETABLE_PR8_ASYNC_JOB_UI_ENABLED=True,
    )
    def test_effective_when_both_on(self) -> None:
        from core.services.pr8_async_job_ui import is_async_job_ui_effective

        self.assertTrue(is_async_job_ui_effective())


class TestAcceptancePack(SimpleTestCase):
    """c7 — three scripted status-progression fixtures render correctly."""

    def test_happy_path_progression_renderable(self) -> None:
        from core.services.pr8_async_job_ui import render_progression_snapshot

        snap = render_progression_snapshot("happy")
        self.assertEqual(
            [s["status"] for s in snap],
            ["queued", "running", "succeeded"],
        )

    def test_cancelled_path_progression_renderable(self) -> None:
        from core.services.pr8_async_job_ui import render_progression_snapshot

        snap = render_progression_snapshot("cancelled")
        self.assertEqual(snap[-1]["status"], "cancelled")

    def test_failed_path_progression_renderable(self) -> None:
        from core.services.pr8_async_job_ui import render_progression_snapshot

        snap = render_progression_snapshot("failed")
        self.assertEqual(snap[-1]["status"], "failed")


class TestParityHelper(SimpleTestCase):
    """c7 — parity helper strips PR8 context keys only."""

    def test_strip_preserves_unrelated_context(self) -> None:
        from core.services.pr8_parity import strip_pr8_ui_context

        ctx = {
            "scenario": "x",
            "pr8_job": {"status": "running"},
            "TIMETABLE_PR8_ASYNC_JOB_UI_ENABLED": True,
        }
        out = strip_pr8_ui_context(ctx)
        self.assertIn("scenario", out)
        self.assertNotIn("pr8_job", out)


class TestFlagDefaultPostPromotion(SimpleTestCase):
    """c8 — promotion flips default to True."""

    def test_default_after_promotion(self) -> None:
        from django.conf import settings

        self.assertTrue(getattr(settings, PR8_FLAG, False))

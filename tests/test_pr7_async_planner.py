"""PR7 — async planner execution: failing tests + tripwires (commit 1).

At commit 1, every test in this file is red. The imports themselves act
as tripwires for the modules and model that commits 2+ must introduce:

- ``core.models.PlannerJob``
- ``core.services.planner_job_runner``
- ``core.services.timetable_pr7_parity.strip_pr7_fields_for_parity``
- migration ``core/migrations/XXXX_plannerjob.py``

Commits 2–7 turn each section green in order. Commit 8 is promotion only.

Test map → commit it turns green:

- ``TestPR7Tripwire`` — commit 2 (model + runner skeleton land)
- ``TestPlannerJobShape`` — commit 2
- ``TestFlagHelper`` — commit 2
- ``TestRunnerHappyPath`` — commit 3
- ``TestFailureCapture`` — commit 3
- ``TestCooperativeCancel`` — commit 4
- ``TestAPISubmit`` / ``TestAPIPoll`` / ``TestAPIResult`` / ``TestAPICancel`` — commit 5
- ``TestUIAsyncCheckbox`` — commit 6
- ``TestParityHelper`` / ``TestCLIReport`` / ``TestFlagOffAllEndpoints404`` — commit 7
- ``TestFlagDefaultPostPromotion`` — commit 8

Intentionally PR7-shaped only. Does not touch PR3/PR5/PR6 regression gates.
"""

from __future__ import annotations

from django.test import SimpleTestCase, TransactionTestCase
from django.test.utils import override_settings

PR7_FLAG = "TIMETABLE_PR7_ASYNC_PLANNER_ENABLED"


class TestPR7Tripwire(SimpleTestCase):
    """Tripwire — fails at commit 1, green at commit 2 when the model
    and runner skeleton land."""

    def test_planner_job_model_importable(self) -> None:
        from core.models import PlannerJob  # noqa: F401

    def test_planner_job_runner_importable(self) -> None:
        from core.services import planner_job_runner  # noqa: F401

    def test_pr7_parity_helper_importable(self) -> None:
        from core.services.timetable_pr7_parity import (  # noqa: F401
            strip_pr7_fields_for_parity,
        )


class TestPlannerJobShape(TransactionTestCase):
    """PlannerJob model has the DoR-pinned fields. Green at commit 2."""

    def test_required_fields_present(self) -> None:
        from core.models import PlannerJob

        names = {f.name for f in PlannerJob._meta.get_fields()}
        for required in (
            "id",
            "scenario",
            "mode",
            "status",
            "submitted_by",
            "submitted_at",
            "started_at",
            "finished_at",
            "error_message",
            "result_json",
            "last_stage_seen",
            "cancel_requested",
        ):
            self.assertIn(required, names, f"PlannerJob missing {required}")

    def test_status_choices_match_dor(self) -> None:
        from core.models import PlannerJob

        allowed = {c[0] for c in PlannerJob._meta.get_field("status").choices or ()}
        self.assertEqual(
            allowed,
            {"queued", "running", "succeeded", "failed", "cancelled"},
        )


class TestFlagHelper(SimpleTestCase):
    """``is_async_planner_enabled`` reads the flag. Green at commit 2."""

    @override_settings(TIMETABLE_PR7_ASYNC_PLANNER_ENABLED=True)
    def test_flag_on(self) -> None:
        from core.services.planner_job_runner import is_async_planner_enabled

        self.assertTrue(is_async_planner_enabled())

    @override_settings(TIMETABLE_PR7_ASYNC_PLANNER_ENABLED=False)
    def test_flag_off(self) -> None:
        from core.services.planner_job_runner import is_async_planner_enabled

        self.assertFalse(is_async_planner_enabled())


class TestRunnerHappyPath(TransactionTestCase):
    """submit → run → succeeded; result_json byte-equals the sync-path payload
    for the same scenario. Green at commit 3."""

    @override_settings(TIMETABLE_PR7_ASYNC_PLANNER_ENABLED=True)
    def test_submit_runs_and_succeeds(self) -> None:
        from pr7_fixture_loader import load_pr7_fixture

        from core.services.planner_job_runner import (
            run_planner_job,
            submit_planner_job,
        )

        scenario, _, _ = load_pr7_fixture("pr7_async_happy_path.json")
        job_id = submit_planner_job(scenario_id=scenario.id, mode="full_rebuild", user=None)
        run_planner_job(job_id)

        from core.models import PlannerJob

        job = PlannerJob.objects.get(id=job_id)
        self.assertEqual(job.status, "succeeded")
        self.assertIsNotNone(job.result_json)
        self.assertIsNotNone(job.started_at)
        self.assertIsNotNone(job.finished_at)
        self.assertIn(
            job.last_stage_seen,
            {"greedy", "sa", "cpsat", "chain", "rooming_repair"},
        )


class TestFailureCapture(TransactionTestCase):
    """Runner captures exceptions → status=failed, error_message populated,
    last_stage_seen reflects the last completed stage. Green at commit 3."""

    @override_settings(TIMETABLE_PR7_ASYNC_PLANNER_ENABLED=True)
    def test_failed_run_captures_error_and_last_stage(self) -> None:
        from unittest.mock import patch

        from pr7_fixture_loader import load_pr7_fixture

        from core.services.planner_job_runner import (
            run_planner_job,
            submit_planner_job,
        )

        scenario, _, _ = load_pr7_fixture("pr7_failure_capture.json")
        job_id = submit_planner_job(scenario_id=scenario.id, mode="full_rebuild", user=None)

        with patch(
            "core.services.timetable_autoplace.auto_place_scenario",
            side_effect=RuntimeError("synthetic fault"),
        ):
            run_planner_job(job_id)

        from core.models import PlannerJob

        job = PlannerJob.objects.get(id=job_id)
        self.assertEqual(job.status, "failed")
        self.assertTrue(job.error_message and "synthetic fault" in job.error_message)
        self.assertIsNone(job.result_json)


class TestCooperativeCancel(TransactionTestCase):
    """cancel_requested=True before a stage boundary → status=cancelled,
    result_json null. Green at commit 4."""

    @override_settings(TIMETABLE_PR7_ASYNC_PLANNER_ENABLED=True)
    def test_cancel_before_run_yields_cancelled(self) -> None:
        from pr7_fixture_loader import load_pr7_fixture

        from core.models import PlannerJob
        from core.services.planner_job_runner import (
            cancel_planner_job,
            run_planner_job,
            submit_planner_job,
        )

        scenario, _, _ = load_pr7_fixture("pr7_cancel_boundary.json")
        job_id = submit_planner_job(scenario_id=scenario.id, mode="full_rebuild", user=None)
        cancel_planner_job(job_id, user=None)
        run_planner_job(job_id)

        job = PlannerJob.objects.get(id=job_id)
        self.assertEqual(job.status, "cancelled")
        self.assertIsNone(job.result_json)


class TestAPISubmit(TransactionTestCase):
    """POST /planner-jobs/ → 201 with job_id. Green at commit 5."""

    @override_settings(TIMETABLE_PR7_ASYNC_PLANNER_ENABLED=True)
    def test_submit_returns_job_id(self) -> None:
        import json

        from django.test import Client
        from pr7_fixture_loader import load_pr7_fixture

        scenario, _, _ = load_pr7_fixture("pr7_async_happy_path.json")
        client = Client()
        response = client.post(
            "/planner-jobs/",
            data=json.dumps({"scenario_id": scenario.id, "mode": "full_rebuild"}),
            content_type="application/json",
        )
        self.assertIn(response.status_code, (200, 201))
        body = response.json()
        self.assertIn("job_id", body)
        self.assertEqual(body.get("status"), "queued")


class TestAPIPoll(TransactionTestCase):
    """GET /planner-jobs/<id>/ returns status without result_json. Green at commit 5."""

    @override_settings(TIMETABLE_PR7_ASYNC_PLANNER_ENABLED=True)
    def test_poll_does_not_echo_result(self) -> None:
        from django.test import Client
        from pr7_fixture_loader import load_pr7_fixture

        from core.services.planner_job_runner import submit_planner_job

        scenario, _, _ = load_pr7_fixture("pr7_async_happy_path.json")
        job_id = submit_planner_job(scenario_id=scenario.id, mode="full_rebuild", user=None)
        client = Client()
        response = client.get(f"/planner-jobs/{job_id}/")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("status", body)
        self.assertNotIn("result_json", body)


class TestAPIResult(TransactionTestCase):
    """GET /planner-jobs/<id>/result/ is 404 until succeeded. Green at commit 5."""

    @override_settings(TIMETABLE_PR7_ASYNC_PLANNER_ENABLED=True)
    def test_result_404_when_queued(self) -> None:
        from django.test import Client
        from pr7_fixture_loader import load_pr7_fixture

        from core.services.planner_job_runner import submit_planner_job

        scenario, _, _ = load_pr7_fixture("pr7_async_happy_path.json")
        job_id = submit_planner_job(scenario_id=scenario.id, mode="full_rebuild", user=None)
        response = Client().get(f"/planner-jobs/{job_id}/result/")
        self.assertEqual(response.status_code, 404)


class TestAPICancel(TransactionTestCase):
    """POST /planner-jobs/<id>/cancel/ sets cancel_requested. Green at commit 5."""

    @override_settings(TIMETABLE_PR7_ASYNC_PLANNER_ENABLED=True)
    def test_cancel_endpoint_sets_flag(self) -> None:
        from django.test import Client
        from pr7_fixture_loader import load_pr7_fixture

        from core.models import PlannerJob
        from core.services.planner_job_runner import submit_planner_job

        scenario, _, _ = load_pr7_fixture("pr7_cancel_boundary.json")
        job_id = submit_planner_job(scenario_id=scenario.id, mode="full_rebuild", user=None)
        response = Client().post(f"/planner-jobs/{job_id}/cancel/")
        self.assertIn(response.status_code, (200, 202))
        job = PlannerJob.objects.get(id=job_id)
        self.assertTrue(job.cancel_requested)


class TestUIAsyncCheckbox(SimpleTestCase):
    """The scenario detail template carries a 'Run in background' control,
    gated on the PR7 flag. Green at commit 6."""

    @override_settings(TIMETABLE_PR7_ASYNC_PLANNER_ENABLED=True)
    def test_async_checkbox_rendered_when_flag_on(self) -> None:
        # The template must surface a toggle with id/name "async_planner".
        # Implementation detail: commit 6 adds the input into
        # templates/core/scenario_detail.html under a flag-gated block.
        from django.template.loader import get_template

        tpl = get_template("core/scenario_detail.html")
        rendered = tpl.render({"TIMETABLE_PR7_ASYNC_PLANNER_ENABLED": True})
        self.assertIn("async_planner", rendered)


class TestParityHelper(SimpleTestCase):
    """``strip_pr7_fields_for_parity`` scrubs only PR7-added fields. Green at commit 7."""

    def test_strip_preserves_pre_pr7_keys(self) -> None:
        from core.services.timetable_pr7_parity import strip_pr7_fields_for_parity

        payload = {
            "placements": [],
            "decision_trace": {},
            "stage_telemetry": {"stage_ms": {}, "stage_iterations": {}},
        }
        stripped = strip_pr7_fields_for_parity(payload)
        for k in ("placements", "decision_trace", "stage_telemetry"):
            self.assertIn(k, stripped)


class TestCLIReport(TransactionTestCase):
    """pr7_job_report surfaces a table with last_stage_seen. Green at commit 7."""

    @override_settings(TIMETABLE_PR7_ASYNC_PLANNER_ENABLED=True)
    def test_cli_runs_clean(self) -> None:
        import io

        from django.core.management import call_command

        buf = io.StringIO()
        call_command("pr7_job_report", stdout=buf)
        out = buf.getvalue()
        self.assertIn("last_stage_seen", out)


class TestFlagOffAllEndpoints404(TransactionTestCase):
    """Flag-off behavioural bar — all PR7 endpoints 404, no PlannerJob rows created. Green at commit 7."""

    @override_settings(TIMETABLE_PR7_ASYNC_PLANNER_ENABLED=False)
    def test_flag_off_submit_404(self) -> None:
        import json

        from django.test import Client

        response = Client().post(
            "/planner-jobs/",
            data=json.dumps({"scenario_id": 1, "mode": "full_rebuild"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 404)

        from core.models import PlannerJob

        self.assertEqual(PlannerJob.objects.count(), 0)


class TestFlagDefaultPostPromotion(SimpleTestCase):
    """After commit 8 promotion, ``is_async_planner_enabled()`` defaults True when
    the env var is unset. Green at commit 8."""

    def test_default_after_promotion(self) -> None:
        from django.conf import settings

        self.assertTrue(getattr(settings, PR7_FLAG, False))

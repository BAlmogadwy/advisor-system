"""Spawn entry point for the PostgreSQL planner crash tests."""

from unittest.mock import patch


def run_worker(db, job_id, phase, pipe):
    # Spawn starts a fresh interpreter. Use the parent's Django test database.
    import django
    from django.conf import settings
    from django.db import connections

    connections.close_all()
    connections["default"].settings_dict.update(db)
    settings.DATABASES["default"].update(db)
    django.setup()
    # Exercise installations that explicitly place the temporary schema last.
    with connections["default"].cursor() as cursor:
        cursor.execute(
            "SELECT set_config('search_path', current_setting('search_path') || ', pg_temp', false)"
        )
    from core.models import (
        DeliveryBoard,
        PlannerJob,
        SectionPlacement,
        TermSection,
        TermSectionMeeting,
    )
    from core.services.planner_job_runner import run_planner_job
    from core.services.timetable_autoplace import auto_place_scenario

    def pause():
        pipe.send("ready")
        assert pipe.poll(60)
        pipe.recv()

    def pause_after_real_candidate(*args, **kwargs):
        result = auto_place_scenario(*args, **kwargs)
        pause()
        return result

    def solve(scenario_id, *args, **kwargs):
        section = TermSection.objects.get(scenario_id=scenario_id)
        board = DeliveryBoard.objects.get(scenario_id=scenario_id)
        SectionPlacement.objects.filter(board=board).delete()
        TermSectionMeeting.objects.filter(term_section=section).delete()
        for model, refs in [(SectionPlacement, {"board": board}), (TermSectionMeeting, {})]:
            model.objects.create(
                term_section=section,
                day="TUE",
                start_time="12:00",
                end_time="13:00",
                room="NEW",
                **refs,
            )
        if "progress" in kwargs:
            kwargs["progress"]("persist")
        if phase == "solve":
            pause()
        return {"placed": 1}

    saved = PlannerJob.save

    def save(job, *args, **kwargs):
        saved(job, *args, **kwargs)
        if phase == "commit" and job.status == PlannerJob.STATUS_SUCCEEDED:
            pause()

    try:
        if phase == "v2_candidate":
            with patch(
                "core.services.timetable_autoplace.auto_place_scenario", pause_after_real_candidate
            ):
                run_planner_job(job_id)
        else:
            with (
                patch("core.services.timetable_autoplace.auto_place_scenario", solve),
                patch("core.services.timetable_v2_runner.run_v2_optimisation_guarded", solve),
                patch("scheduler.bridge.build_into_scenario", solve),
                patch.object(PlannerJob, "save", save),
            ):
                run_planner_job(job_id)
        pipe.send("finished")
    finally:
        connections.close_all()
        pipe.close()

from django.apps import AppConfig


class SchedulerConfig(AppConfig):
    """The new timetabling subsystem.

    Deliberately isolated from ``core.services.timetable_*``: it shares no code,
    no tables and no state with the existing engine, so nothing here can regress
    the timetable the registrar uses today.
    """

    default_auto_field = "django.db.models.BigAutoField"
    name = "scheduler"
    verbose_name = "Scheduler (new timetabling subsystem)"

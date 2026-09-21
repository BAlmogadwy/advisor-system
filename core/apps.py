from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "core"

    def ready(self) -> None:
        from django.db.backends.signals import connection_created

        def _enable_wal(sender: object, connection: object, **kwargs: object) -> None:
            settings_dict = getattr(connection, "settings_dict", {})
            if getattr(connection, "vendor", None) == "sqlite" and not settings_dict.get(
                "RELEASE_SEED_READ_ONLY"
            ):
                cursor = connection.cursor()  # type: ignore[attr-defined]
                cursor.execute("PRAGMA journal_mode=WAL;")
                cursor.execute("PRAGMA synchronous=NORMAL;")

        connection_created.connect(_enable_wal)

        # NOTE: there is deliberately no ProgrammeRequirement cache-invalidation
        # signal here. ``timetable_course_tier.program_count_by_code`` used to be
        # ``lru_cache``d with these signals clearing it, but a process-local cache
        # cannot be invalidated across gunicorn workers: only the worker serving
        # the write cleared, leaving the others computing a different course-tier
        # map for the rest of their lifetime. The function now reads fresh (it
        # runs once per optimise run, not per evaluation), so no invalidation is
        # required. See that function's docstring before reintroducing a cache.

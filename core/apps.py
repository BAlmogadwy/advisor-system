from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "core"

    def ready(self) -> None:
        from django.db.backends.signals import connection_created

        def _enable_wal(sender: object, connection: object, **kwargs: object) -> None:
            if getattr(connection, "vendor", None) == "sqlite":
                cursor = connection.cursor()  # type: ignore[attr-defined]
                cursor.execute("PRAGMA journal_mode=WAL;")
                cursor.execute("PRAGMA synchronous=NORMAL;")

        connection_created.connect(_enable_wal)

        # Invalidate the cached course→plan-count map whenever ProgrammeRequirement
        # rows change, so the tiered objective never serves a stale course tier
        # after a curriculum re-import on a long-lived worker. Covers every write
        # path (update_or_create, queryset.delete) via signals. bulk_create in the
        # bootstrap migration is one-time pre-boot, so a fresh cache follows it.
        from django.db.models.signals import post_delete, post_save

        from core.models import ProgrammeRequirement
        from core.services.timetable_course_tier import clear_program_count_cache

        def _clear_tier_cache(sender: object, **kwargs: object) -> None:
            clear_program_count_cache()

        post_save.connect(
            _clear_tier_cache, sender=ProgrammeRequirement, dispatch_uid="tier_cache_ps"
        )
        post_delete.connect(
            _clear_tier_cache, sender=ProgrammeRequirement, dispatch_uid="tier_cache_pd"
        )

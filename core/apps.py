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

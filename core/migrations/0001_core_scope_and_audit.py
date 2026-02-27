from django.db import migrations


def create_tables(apps, schema_editor):
    """Create core_user_scope and core_audit_log tables (vendor-aware)."""
    vendor = schema_editor.connection.vendor

    schema_editor.execute("""
        CREATE TABLE IF NOT EXISTS core_user_scope (
            user_id INTEGER PRIMARY KEY,
            advisor_id TEXT,
            departments TEXT,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES auth_user(id) ON DELETE CASCADE
        );
    """)

    if vendor == "sqlite":
        schema_editor.execute("""
            CREATE TABLE IF NOT EXISTS core_audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_utc TEXT NOT NULL,
                actor_username TEXT,
                actor_role TEXT,
                action TEXT NOT NULL,
                endpoint TEXT,
                method TEXT,
                status TEXT,
                details_json TEXT,
                error_text TEXT,
                prev_hash TEXT,
                entry_hash TEXT
            );
        """)
    else:
        schema_editor.execute("""
            CREATE TABLE IF NOT EXISTS core_audit_log (
                id SERIAL PRIMARY KEY,
                ts_utc TEXT NOT NULL,
                actor_username TEXT,
                actor_role TEXT,
                action TEXT NOT NULL,
                endpoint TEXT,
                method TEXT,
                status TEXT,
                details_json TEXT,
                error_text TEXT,
                prev_hash TEXT,
                entry_hash TEXT
            );
        """)


def drop_tables(apps, schema_editor):
    schema_editor.execute("DROP TABLE IF EXISTS core_audit_log;")
    schema_editor.execute("DROP TABLE IF EXISTS core_user_scope;")


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    operations = [
        migrations.RunPython(create_tables, drop_tables),
    ]

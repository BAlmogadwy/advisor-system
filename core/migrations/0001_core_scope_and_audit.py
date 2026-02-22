from django.db import migrations


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    operations = [
        migrations.RunSQL(
            sql="""
            CREATE TABLE IF NOT EXISTS core_user_scope (
                user_id INTEGER PRIMARY KEY,
                advisor_id TEXT,
                departments TEXT,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES auth_user(id) ON DELETE CASCADE
            );
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
            """,
            reverse_sql="""
            DROP TABLE IF EXISTS core_audit_log;
            DROP TABLE IF EXISTS core_user_scope;
            """,
        ),
    ]
